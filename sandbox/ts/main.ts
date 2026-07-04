#!/usr/bin/env node
// In-container verification entrypoint — TypeScript/Node.js implementation.
//
// Phase-8 TypeScript conforming runner. Implements the IDENTICAL pipeline contract
// as sandbox/entrypoint.py (apply -> build -> test) and emits the SAME
// sentinel-delimited JSON result shape. The host-side TsSandboxRunner invokes
// the Docker image built from this file; it never calls this code directly.
//
// Contract:
//   - Reads patch from /patch/patch.json (read-only bind)
//   - Copies /snapshot (read-only) into /work/repo (writable tmpfs)
//   - Applies patch (whole-file write/delete ops, path-traversal-safe)
//   - Build gate: python3 -m compileall (same gate as Python runner)
//   - Test: python3 -m pytest
//   - Emits sentinel-delimited JSON (last occurrence wins against spoofed output)

import * as fs from "fs";
import * as path from "path";
import { spawnSync } from "child_process";

const RESULT_BEGIN = "<<<ACP_RESULT_BEGIN>>>";
const RESULT_END = "<<<ACP_RESULT_END>>>";
const SNAPSHOT_RO = "/snapshot";
const PATCH_PATH = "/patch/patch.json";
const WORK_DIR = "/work";
const MAX_CAPTURE = 16000;

interface PatchOp {
  op: string;
  path: string;
  content?: string;
}

interface PatchEnvelope {
  ops: PatchOp[];
}

interface Result {
  applied: boolean;
  built: boolean;
  tests_passed: boolean;
  exit_code: number;
  stdout_tail: string;
  stderr_tail: string;
  stage: string;
  wall_clock_seconds: number;
}

function truncate(s: string): string {
  if (s.length <= MAX_CAPTURE) return s;
  const half = MAX_CAPTURE / 2;
  return s.slice(0, half) + "\n...[truncated]...\n" + s.slice(s.length - half);
}

function emit(r: Result): void {
  process.stdout.write(RESULT_BEGIN + "\n");
  process.stdout.write(JSON.stringify(r) + "\n");
  process.stdout.write(RESULT_END + "\n");
}

function copyDir(src: string, dst: string): void {
  if (!fs.existsSync(dst)) fs.mkdirSync(dst, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath = path.join(src, entry.name);
    const dstPath = path.join(dst, entry.name);
    if (entry.isDirectory()) {
      copyDir(srcPath, dstPath);
    } else {
      fs.copyFileSync(srcPath, dstPath);
    }
  }
}

function applyPatch(work: string): [boolean, string] {
  let data: string;
  try {
    data = fs.readFileSync(PATCH_PATH, "utf-8");
  } catch (e) {
    return [false, `patch envelope unreadable: ${e}`];
  }

  let env: PatchEnvelope;
  try {
    env = JSON.parse(data) as PatchEnvelope;
  } catch (e) {
    return [false, `patch envelope invalid JSON: ${e}`];
  }

  if (!Array.isArray(env.ops)) {
    return [false, "patch envelope missing 'ops' list"];
  }

  const workResolved = fs.realpathSync(work);

  for (const op of env.ops) {
    if (!op.op || !op.path) {
      return [false, `malformed op: ${JSON.stringify(op)}`];
    }
    const target = path.join(work, op.path);
    // Resolve the parent dir (which must exist for traversal check on new files).
    const parentDir = path.dirname(target);
    let targetResolved: string;
    try {
      // If target doesn't exist yet, canonicalize parent and append filename.
      if (fs.existsSync(target)) {
        targetResolved = fs.realpathSync(target);
      } else if (fs.existsSync(parentDir)) {
        targetResolved = path.join(fs.realpathSync(parentDir), path.basename(target));
      } else {
        targetResolved = path.resolve(target);
      }
    } catch {
      targetResolved = path.resolve(target);
    }

    // Fail closed on path traversal out of work dir.
    if (
      targetResolved !== workResolved &&
      !targetResolved.startsWith(workResolved + path.sep)
    ) {
      return [false, `patch path escapes work dir: ${op.path}`];
    }

    switch (op.op) {
      case "write": {
        const content = op.content ?? "";
        fs.mkdirSync(path.dirname(target), { recursive: true });
        fs.writeFileSync(target, content, "utf-8");
        break;
      }
      case "delete":
        if (fs.existsSync(target)) fs.unlinkSync(target);
        break;
      default:
        return [false, `unknown op kind: ${JSON.stringify(op.op)}`];
    }
  }
  return [true, `applied ${env.ops.length} op(s)`];
}

function build(work: string): [boolean, string] {
  const res = spawnSync("python3", ["-m", "compileall", "-q", work], {
    encoding: "utf-8",
  });
  if (res.status === 0) return [true, "compileall ok"];
  const combined = (res.stdout ?? "") + (res.stderr ?? "");
  return [false, truncate(combined)];
}

function runTests(work: string): [number, string, string] {
  const backend = path.join(work, "backend");
  const cwd = fs.existsSync(backend) ? backend : work;
  const res = spawnSync(
    "python3",
    ["-m", "pytest", "-q", "--no-header", cwd],
    {
      cwd,
      encoding: "utf-8",
      env: {
        ...process.env,
        PYTHONPATH: cwd,
        PYTHONDONTWRITEBYTECODE: "1",
      },
    }
  );
  // status is null when the process was killed by a signal.
  // Translate to the negative signal number (same convention as Python's
  // subprocess.returncode) so the host-side OOM classifier sees -9 (SIGKILL).
  let exitCode: number;
  if (res.status !== null) {
    exitCode = res.status;
  } else if (res.signal) {
    // "SIGKILL" -> -9, "SIGTERM" -> -15, etc.
    const sigMap: Record<string, number> = {
      SIGKILL: -9, SIGTERM: -15, SIGABRT: -6, SIGSEGV: -11,
    };
    exitCode = sigMap[res.signal] ?? -1;
  } else {
    exitCode = 1;
  }
  return [exitCode, truncate(res.stdout ?? ""), truncate(res.stderr ?? "")];
}

function main(): void {
  const started = Date.now();
  const r: Result = {
    applied: false,
    built: false,
    tests_passed: false,
    exit_code: -1,
    stdout_tail: "",
    stderr_tail: "",
    stage: "apply",
    wall_clock_seconds: 0,
  };

  const elapsed = () => (Date.now() - started) / 1000;

  try {
    fs.mkdirSync(WORK_DIR, { recursive: true });
  } catch (e) {
    r.stderr_tail = `cannot create work dir: ${e}`;
    r.wall_clock_seconds = elapsed();
    emit(r);
    return;
  }

  const repo = path.join(WORK_DIR, "repo");
  try {
    copyDir(SNAPSHOT_RO, repo);
  } catch (e) {
    r.stderr_tail = `copy snapshot failed: ${e}`;
    r.wall_clock_seconds = elapsed();
    emit(r);
    return;
  }

  const [applied, applyDetail] = applyPatch(repo);
  r.applied = applied;
  if (!applied) {
    r.stderr_tail = truncate(applyDetail);
    r.wall_clock_seconds = elapsed();
    emit(r);
    return;
  }

  r.stage = "build";
  const [built, buildDetail] = build(repo);
  r.built = built;
  if (!built) {
    r.stderr_tail = truncate(buildDetail);
    r.wall_clock_seconds = elapsed();
    emit(r);
    return;
  }

  r.stage = "test";
  const [exitCode, out, err] = runTests(repo);
  r.exit_code = exitCode;
  r.tests_passed = exitCode === 0;
  r.stdout_tail = truncate(out);
  r.stderr_tail = truncate(err);
  r.wall_clock_seconds = elapsed();
  emit(r);
}

main();

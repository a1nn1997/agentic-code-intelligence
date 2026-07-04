#!/usr/bin/env python3
"""In-container verification entrypoint — runs UNTRUSTED code in isolation.

This script is the process that executes inside the Docker sandbox. It is the
only code that ever touches the patch or the repo's own test suite, and it does
so entirely within a writable **tmpfs** work directory. The host filesystem is
mounted read-only (or not at all); nothing this script does can mutate the host.

Pipeline (apply -> build -> test), each stage gated on the previous:

  1. apply  : copy the read-only snapshot into the tmpfs work dir, then apply
              the patch envelope. A patch that does not apply => applied=false,
              pipeline stops. (No git/patch binary needed — the envelope is a
              small owned JSON format of whole-file operations, so application
              is deterministic and tool-free.)
  2. build  : `python -m compileall` over the repo = a real syntax/build gate.
              A file that does not compile => built=false, pipeline stops.
  3. test   : run the repo's own pytest suite. Non-zero exit => tests_passed
              =false, with the REAL captured stdout/stderr returned.

The result is emitted as a single JSON object on a sentinel-delimited line so
the host runner can parse it unambiguously even if the repo's tests print noise.

Everything here treats the repo + patch as hostile: no network is available
(the container is started with --network=none), the process runs as a non-root
user with all capabilities dropped, and it can only write to tmpfs.
"""

from __future__ import annotations

import json
import os
import py_compile
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Sentinel that brackets the machine-readable result on stdout. The repo's own
# test output is untrusted and may contain anything, so the host parses only the
# text between these markers (last occurrence wins).
RESULT_BEGIN = "<<<ACP_RESULT_BEGIN>>>"
RESULT_END = "<<<ACP_RESULT_END>>>"

# Read-only mount of the snapshot the host prepared, and the patch envelope.
SNAPSHOT_RO = Path("/snapshot")          # read-only bind of the workspace copy
PATCH_PATH = Path("/patch/patch.json")   # read-only bind of the patch envelope
WORK = Path("/work")                     # tmpfs, the ONLY writable location

# Truncate captured output so a pathological test cannot exhaust memory via the
# result payload. The head+tail of real failures is what the repair loop needs.
_MAX_CAPTURE = 16_000


def _truncate(text: str) -> str:
    if len(text) <= _MAX_CAPTURE:
        return text
    half = _MAX_CAPTURE // 2
    return text[:half] + "\n...[truncated]...\n" + text[-half:]


def _emit(result: dict[str, object]) -> None:
    sys.stdout.write(RESULT_BEGIN + "\n")
    sys.stdout.write(json.dumps(result))
    sys.stdout.write("\n" + RESULT_END + "\n")
    sys.stdout.flush()


def _apply_patch(work: Path) -> tuple[bool, str]:
    """Apply the owned patch envelope into ``work``. Returns (applied, detail).

    Envelope schema (stable contract shared with Phase 8 runners):
        {"ops": [
            {"op": "write",  "path": "rel/path", "content": "..."},
            {"op": "delete", "path": "rel/path"}
        ]}
    Paths are repo-relative; any attempt to escape ``work`` fails closed.
    """
    try:
        envelope = json.loads(PATCH_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - untrusted input, report cleanly
        return False, f"patch envelope unreadable: {exc}"

    ops = envelope.get("ops")
    if not isinstance(ops, list):
        return False, "patch envelope missing 'ops' list"

    work_resolved = work.resolve()
    for op in ops:
        try:
            kind = op["op"]
            rel = op["path"]
        except (KeyError, TypeError) as exc:
            return False, f"malformed op {op!r}: {exc}"
        target = (work / rel).resolve()
        # Fail closed on path traversal out of the work dir.
        if work_resolved != target and work_resolved not in target.parents:
            return False, f"patch path escapes work dir: {rel}"
        if kind == "write":
            content = op.get("content", "")
            if not isinstance(content, str):
                return False, f"write op content must be str for {rel}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        elif kind == "delete":
            if target.exists():
                target.unlink()
        else:
            return False, f"unknown op kind: {kind!r}"
    return True, f"applied {len(ops)} op(s)"


def _build(work: Path) -> tuple[bool, str]:
    """Syntax/build gate: byte-compile every .py in the repo. Real failure text
    (file + line + SyntaxError) is captured and returned on failure."""
    errors: list[str] = []
    for py in sorted(work.rglob("*.py")):
        try:
            py_compile.compile(str(py), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(str(exc))
    if errors:
        return False, "\n".join(errors)
    return True, "compileall ok"


def _test(work: Path) -> tuple[int, str, str]:
    """Run the repo's own pytest suite. Returns (exit_code, stdout, stderr).

    Runs from the backend package root so `import app...` resolves the same way
    the fixture's tests expect. All output is captured — this is the real
    failure text the Phase-5 self-repair loop reads."""
    backend = work / "backend"
    cwd = backend if backend.is_dir() else work
    env = dict(os.environ)
    # Make the backend importable and keep pytest from touching $HOME/network.
    env["PYTHONPATH"] = str(cwd)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", str(cwd)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def main() -> int:
    started = time.monotonic()
    result: dict[str, object] = {
        "applied": False,
        "built": False,
        "tests_passed": False,
        "exit_code": -1,
        "stdout_tail": "",
        "stderr_tail": "",
        "stage": "apply",
    }

    WORK.mkdir(parents=True, exist_ok=True)
    repo = WORK / "repo"
    # Copy the read-only snapshot into writable tmpfs. The host tree is never
    # touched; all mutation happens on this copy.
    shutil.copytree(SNAPSHOT_RO, repo)

    applied, apply_detail = _apply_patch(repo)
    result["applied"] = applied
    if not applied:
        result["stderr_tail"] = _truncate(apply_detail)
        result["wall_clock_seconds"] = round(time.monotonic() - started, 3)
        _emit(result)
        return 0

    result["stage"] = "build"
    built, build_detail = _build(repo)
    result["built"] = built
    if not built:
        result["stderr_tail"] = _truncate(build_detail)
        result["wall_clock_seconds"] = round(time.monotonic() - started, 3)
        _emit(result)
        return 0

    result["stage"] = "test"
    exit_code, out, err = _test(repo)
    result["exit_code"] = exit_code
    result["tests_passed"] = exit_code == 0
    result["stdout_tail"] = _truncate(out)
    result["stderr_tail"] = _truncate(err)
    result["wall_clock_seconds"] = round(time.monotonic() - started, 3)
    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())

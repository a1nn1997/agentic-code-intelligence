// In-container verification entrypoint — Rust implementation (stdlib-only).
//
// Phase-8 Rust conforming runner. Implements the IDENTICAL pipeline contract as
// sandbox/entrypoint.py (apply -> build -> test) and emits the SAME
// sentinel-delimited JSON result shape. Uses only the Rust standard library —
// no crates.io dependencies — so the binary compiles inside Docker without any
// network access.
//
// Contract:
//   - Reads patch from /patch/patch.json (read-only bind)
//   - Copies /snapshot (read-only) into /work/repo (writable tmpfs)
//   - Applies patch (whole-file write/delete ops, path-traversal-safe)
//   - Build gate: python3 -m compileall (same gate as Python runner)
//   - Test: python3 -m pytest
//   - Emits sentinel-delimited JSON (last occurrence wins against spoofed output)

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Instant;

const RESULT_BEGIN: &str = "<<<ACP_RESULT_BEGIN>>>";
const RESULT_END: &str = "<<<ACP_RESULT_END>>>";
const SNAPSHOT_RO: &str = "/snapshot";
const PATCH_PATH: &str = "/patch/patch.json";
const WORK_DIR: &str = "/work";
const MAX_CAPTURE: usize = 16000;

struct RunResult {
    applied: bool,
    built: bool,
    tests_passed: bool,
    exit_code: i32,
    stdout_tail: String,
    stderr_tail: String,
    stage: String,
    wall_clock_seconds: f64,
}

fn truncate(s: &str) -> String {
    if s.len() <= MAX_CAPTURE {
        return s.to_string();
    }
    let half = MAX_CAPTURE / 2;
    // Safe byte-boundary truncation for UTF-8.
    let head_end = s.char_indices().nth(half).map(|(i, _)| i).unwrap_or(half);
    let tail_start = s.char_indices().nth_back(half).map(|(i, _)| i).unwrap_or(s.len() - half);
    format!("{}\n...[truncated]...\n{}", &s[..head_end], &s[tail_start..])
}

/// Minimal JSON serializer for the result struct (stdlib-only, no serde).
fn to_json(r: &RunResult) -> String {
    fn esc(s: &str) -> String {
        s.replace('\\', "\\\\")
            .replace('"', "\\\"")
            .replace('\n', "\\n")
            .replace('\r', "\\r")
            .replace('\t', "\\t")
    }
    format!(
        r#"{{"applied":{applied},"built":{built},"tests_passed":{tests_passed},"exit_code":{exit_code},"stdout_tail":"{stdout}","stderr_tail":"{stderr}","stage":"{stage}","wall_clock_seconds":{wall}}}"#,
        applied = r.applied,
        built = r.built,
        tests_passed = r.tests_passed,
        exit_code = r.exit_code,
        stdout = esc(&r.stdout_tail),
        stderr = esc(&r.stderr_tail),
        stage = esc(&r.stage),
        wall = r.wall_clock_seconds,
    )
}

fn emit(r: &RunResult) {
    println!("{}", RESULT_BEGIN);
    println!("{}", to_json(r));
    println!("{}", RESULT_END);
    io::stdout().flush().ok();
}

fn copy_dir(src: &Path, dst: &Path) -> Result<(), String> {
    if !dst.exists() {
        fs::create_dir_all(dst).map_err(|e| e.to_string())?;
    }
    for entry in fs::read_dir(src).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let src_path = entry.path();
        let dst_path = dst.join(entry.file_name());
        if src_path.is_dir() {
            copy_dir(&src_path, &dst_path)?;
        } else {
            fs::copy(&src_path, &dst_path).map_err(|e| {
                format!("copy {:?} -> {:?}: {}", src_path, dst_path, e)
            })?;
        }
    }
    Ok(())
}

/// Minimal JSON string value extractor: finds `"key":"value"` or `"key": "value"`.
fn extract_str(json: &str, key: &str) -> Option<String> {
    let needle = format!("\"{}\"", key);
    let pos = json.find(&needle)?;
    let after_key = &json[pos + needle.len()..];
    // Skip whitespace and colon.
    let after_colon = after_key.trim_start().strip_prefix(':')?.trim_start();
    if after_colon.starts_with('"') {
        let inner = &after_colon[1..];
        let mut result = String::new();
        let mut chars = inner.chars();
        loop {
            match chars.next()? {
                '"' => break,
                '\\' => match chars.next()? {
                    'n' => result.push('\n'),
                    'r' => result.push('\r'),
                    't' => result.push('\t'),
                    c => result.push(c),
                },
                c => result.push(c),
            }
        }
        Some(result)
    } else {
        None
    }
}

fn apply_patch(work: &Path) -> (bool, String) {
    let data = match fs::read_to_string(PATCH_PATH) {
        Ok(d) => d,
        Err(e) => return (false, format!("patch envelope unreadable: {}", e)),
    };

    // Parse the ops array. Minimal parser: find the "ops" key and extract
    // each op object's "op", "path", and "content" fields.
    let ops_start = match data.find("\"ops\"") {
        Some(p) => p,
        None => return (false, "patch envelope missing 'ops' key".to_string()),
    };

    let work_resolved = match fs::canonicalize(work) {
        Ok(p) => p,
        Err(e) => return (false, format!("cannot resolve work dir: {}", e)),
    };

    // Split into individual op-objects by parsing bracket depth.
    let after_ops = &data[ops_start..];
    let array_start = match after_ops.find('[') {
        Some(p) => p,
        None => return (false, "ops is not an array".to_string()),
    };
    let array = &after_ops[array_start..];

    let mut ops: Vec<String> = Vec::new();
    let mut depth = 0i32;
    let mut op_start: Option<usize> = None;
    for (i, ch) in array.char_indices() {
        match ch {
            '{' => {
                depth += 1;
                if depth == 1 {
                    op_start = Some(i);
                }
            }
            '}' => {
                depth -= 1;
                if depth == 0 {
                    if let Some(s) = op_start {
                        ops.push(array[s..=i].to_string());
                        op_start = None;
                    }
                }
            }
            ']' if depth == 0 => break,
            _ => {}
        }
    }

    let mut applied_count = 0usize;
    for op_json in &ops {
        let kind = match extract_str(op_json, "op") {
            Some(k) => k,
            None => return (false, format!("op missing 'op' field in: {}", op_json)),
        };
        let rel = match extract_str(op_json, "path") {
            Some(p) => p,
            None => return (false, format!("op missing 'path' field in: {}", op_json)),
        };
        let target = work.join(&rel);

        // Fail closed on path traversal.
        let canonical_target = if target.exists() {
            match fs::canonicalize(&target) {
                Ok(p) => p,
                Err(_) => target.clone(),
            }
        } else if let Some(parent) = target.parent() {
            let parent_resolved = if parent.exists() {
                fs::canonicalize(parent).unwrap_or_else(|_| parent.to_path_buf())
            } else {
                parent.to_path_buf()
            };
            parent_resolved.join(target.file_name().unwrap_or_default())
        } else {
            target.clone()
        };

        let work_str = work_resolved.to_string_lossy();
        let target_str = canonical_target.to_string_lossy();
        if target_str != work_str
            && !target_str.starts_with(&format!("{}/", work_str))
        {
            return (false, format!("patch path escapes work dir: {}", rel));
        }

        match kind.as_str() {
            "write" => {
                let content = extract_str(op_json, "content").unwrap_or_default();
                if let Some(parent) = target.parent() {
                    if let Err(e) = fs::create_dir_all(parent) {
                        return (false, format!("mkdir {:?}: {}", parent, e));
                    }
                }
                if let Err(e) = fs::write(&target, content.as_bytes()) {
                    return (false, format!("write {:?}: {}", target, e));
                }
            }
            "delete" => {
                if target.exists() {
                    if let Err(e) = fs::remove_file(&target) {
                        return (false, format!("delete {:?}: {}", target, e));
                    }
                }
            }
            _ => return (false, format!("unknown op kind: {:?}", kind)),
        }
        applied_count += 1;
    }
    (true, format!("applied {} op(s)", applied_count))
}

fn build(work: &Path) -> (bool, String) {
    let output = Command::new("python3")
        .args(["-m", "compileall", "-q", &work.to_string_lossy()])
        .output();
    match output {
        Ok(out) if out.status.success() => (true, "compileall ok".to_string()),
        Ok(out) => {
            let combined = String::from_utf8_lossy(&out.stdout).to_string()
                + &String::from_utf8_lossy(&out.stderr);
            (false, truncate(&combined))
        }
        Err(e) => (false, format!("compileall exec error: {}", e)),
    }
}

fn run_tests(work: &Path) -> (i32, String, String) {
    let backend = work.join("backend");
    let cwd: PathBuf = if backend.is_dir() {
        backend.clone()
    } else {
        work.to_path_buf()
    };

    let mut cmd = Command::new("python3");
    cmd.args(["-m", "pytest", "-q", "--no-header", &cwd.to_string_lossy()])
        .current_dir(&cwd)
        .env("PYTHONPATH", &cwd)
        .env("PYTHONDONTWRITEBYTECODE", "1");

    match cmd.output() {
        Ok(out) => {
            // status().code() is None when the process was killed by a signal.
            // On Unix, use the raw signal number negated — the same convention
            // as Python's subprocess.returncode — so the host-side OOM
            // classifier sees -9 for SIGKILL.
            #[cfg(unix)]
            let exit_code = {
                use std::os::unix::process::ExitStatusExt;
                if let Some(code) = out.status.code() {
                    code
                } else if let Some(sig) = out.status.signal() {
                    -sig
                } else {
                    -1
                }
            };
            #[cfg(not(unix))]
            let exit_code = out.status.code().unwrap_or(-1);
            (
                exit_code,
                truncate(&String::from_utf8_lossy(&out.stdout)),
                truncate(&String::from_utf8_lossy(&out.stderr)),
            )
        }
        Err(e) => (-1, String::new(), format!("pytest exec error: {}", e)),
    }
}

fn main() {
    let started = Instant::now();
    let mut r = RunResult {
        applied: false,
        built: false,
        tests_passed: false,
        exit_code: -1,
        stdout_tail: String::new(),
        stderr_tail: String::new(),
        stage: "apply".to_string(),
        wall_clock_seconds: 0.0,
    };

    let work = Path::new(WORK_DIR);
    if let Err(e) = fs::create_dir_all(work) {
        r.stderr_tail = format!("cannot create work dir: {}", e);
        r.wall_clock_seconds = started.elapsed().as_secs_f64();
        emit(&r);
        return;
    }

    let repo = work.join("repo");
    if let Err(e) = copy_dir(Path::new(SNAPSHOT_RO), &repo) {
        r.stderr_tail = format!("copy snapshot failed: {}", e);
        r.wall_clock_seconds = started.elapsed().as_secs_f64();
        emit(&r);
        return;
    }

    let (applied, apply_detail) = apply_patch(&repo);
    r.applied = applied;
    if !applied {
        r.stderr_tail = truncate(&apply_detail);
        r.wall_clock_seconds = started.elapsed().as_secs_f64();
        emit(&r);
        return;
    }

    r.stage = "build".to_string();
    let (built, build_detail) = build(&repo);
    r.built = built;
    if !built {
        r.stderr_tail = truncate(&build_detail);
        r.wall_clock_seconds = started.elapsed().as_secs_f64();
        emit(&r);
        return;
    }

    r.stage = "test".to_string();
    let (exit_code, out, err) = run_tests(&repo);
    r.exit_code = exit_code;
    r.tests_passed = exit_code == 0;
    r.stdout_tail = truncate(&out);
    r.stderr_tail = truncate(&err);
    r.wall_clock_seconds = started.elapsed().as_secs_f64();
    emit(&r);
}

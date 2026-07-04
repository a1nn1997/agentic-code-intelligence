// In-container verification entrypoint — Go implementation.
//
// This is the Phase-8 Go conforming runner. It implements the IDENTICAL pipeline
// contract as sandbox/entrypoint.py (apply -> build -> test) and emits the SAME
// sentinel-delimited JSON result shape. The host-side GoSandboxRunner invokes the
// Docker image built from this file; it never calls this code directly.
//
// Contract:
//   - Reads patch from /patch/patch.json (read-only bind)
//   - Reads snapshot from /snapshot (read-only bind)
//   - Copies snapshot into /work (writable tmpfs), applies patch
//   - Runs build gate (python -m compileall for Python repos)
//   - Runs repo test suite (pytest)
//   - Emits JSON result between <<<ACP_RESULT_BEGIN>>> / <<<ACP_RESULT_END>>>
//   - The host runner reads the LAST occurrence of the sentinel pair
//
// The patch envelope schema is the stable shared contract:
//   {"ops": [{"op": "write"|"delete", "path": "rel/path", "content": "..."}]}
// Paths are repo-relative; any traversal out of /work fails closed.

package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

const (
	resultBegin = "<<<ACP_RESULT_BEGIN>>>"
	resultEnd   = "<<<ACP_RESULT_END>>>"
	snapshotRO  = "/snapshot"
	patchPath   = "/patch/patch.json"
	workDir     = "/work"
	maxCapture  = 16000
)

type patchOp struct {
	Op      string `json:"op"`
	Path    string `json:"path"`
	Content string `json:"content"`
}

type patchEnvelope struct {
	Ops []patchOp `json:"ops"`
}

type result struct {
	Applied          bool    `json:"applied"`
	Built            bool    `json:"built"`
	TestsPassed      bool    `json:"tests_passed"`
	ExitCode         int     `json:"exit_code"`
	StdoutTail       string  `json:"stdout_tail"`
	StderrTail       string  `json:"stderr_tail"`
	Stage            string  `json:"stage"`
	WallClockSeconds float64 `json:"wall_clock_seconds"`
}

func truncate(s string) string {
	if len(s) <= maxCapture {
		return s
	}
	half := maxCapture / 2
	return s[:half] + "\n...[truncated]...\n" + s[len(s)-half:]
}

func emit(r result) {
	data, _ := json.Marshal(r)
	fmt.Printf("%s\n%s\n%s\n", resultBegin, string(data), resultEnd)
}

func copyDir(src, dst string) error {
	return filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		if info.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		return copyFile(path, target)
	})
}

func copyFile(src, dst string) error {
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = io.Copy(out, in)
	return err
}

func applyPatch(work string) (bool, string) {
	data, err := os.ReadFile(patchPath)
	if err != nil {
		return false, fmt.Sprintf("patch envelope unreadable: %v", err)
	}
	var env patchEnvelope
	if err := json.Unmarshal(data, &env); err != nil {
		return false, fmt.Sprintf("patch envelope invalid JSON: %v", err)
	}
	workResolved, err := filepath.Abs(work)
	if err != nil {
		return false, fmt.Sprintf("cannot resolve work dir: %v", err)
	}
	for _, op := range env.Ops {
		target := filepath.Join(work, op.Path)
		targetResolved, err := filepath.Abs(target)
		if err != nil {
			return false, fmt.Sprintf("cannot resolve path %q: %v", op.Path, err)
		}
		// Fail closed on path traversal out of the work dir.
		if targetResolved != workResolved && !strings.HasPrefix(targetResolved, workResolved+string(os.PathSeparator)) {
			return false, fmt.Sprintf("patch path escapes work dir: %s", op.Path)
		}
		switch op.Op {
		case "write":
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return false, fmt.Sprintf("mkdir for %q: %v", op.Path, err)
			}
			if err := os.WriteFile(target, []byte(op.Content), 0o644); err != nil {
				return false, fmt.Sprintf("write %q: %v", op.Path, err)
			}
		case "delete":
			if err := os.Remove(target); err != nil && !os.IsNotExist(err) {
				return false, fmt.Sprintf("delete %q: %v", op.Path, err)
			}
		default:
			return false, fmt.Sprintf("unknown op kind: %q", op.Op)
		}
	}
	return true, fmt.Sprintf("applied %d op(s)", len(env.Ops))
}

func build(work string) (bool, string) {
	// Use python -m compileall for Python repos — same gate as the Python runner.
	cmd := exec.Command("python3", "-m", "compileall", "-q", work)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return false, truncate(string(out))
	}
	return true, "compileall ok"
}

func runTests(work string) (int, string, string) {
	backend := filepath.Join(work, "backend")
	cwd := backend
	if _, err := os.Stat(backend); os.IsNotExist(err) {
		cwd = work
	}
	env := os.Environ()
	env = append(env, "PYTHONPATH="+cwd)
	env = append(env, "PYTHONDONTWRITEBYTECODE=1")
	cmd := exec.Command("python3", "-m", "pytest", "-q", "--no-header", cwd)
	cmd.Dir = cwd
	cmd.Env = env
	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	exitCode := 0
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			exitCode = exitErr.ExitCode()
			// On Unix, ExitCode() returns -1 for signal-terminated processes.
			// Use syscall.WaitStatus to get the actual signal number so the
			// host-side OOM classifier sees -9 (SIGKILL) — the same convention
			// as Python's subprocess.returncode on Unix.
			if exitCode == -1 {
				if ws, ok := exitErr.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
					exitCode = -int(ws.Signal())
				}
			}
		} else {
			exitCode = 1
		}
	}
	return exitCode, stdout.String(), stderr.String()
}

func main() {
	started := time.Now()
	r := result{
		Applied:     false,
		Built:       false,
		TestsPassed: false,
		ExitCode:    -1,
		Stage:       "apply",
	}

	if err := os.MkdirAll(workDir, 0o755); err != nil {
		r.StderrTail = fmt.Sprintf("cannot create work dir: %v", err)
		r.WallClockSeconds = time.Since(started).Seconds()
		emit(r)
		return
	}

	repo := filepath.Join(workDir, "repo")
	if err := copyDir(snapshotRO, repo); err != nil {
		r.StderrTail = fmt.Sprintf("copy snapshot failed: %v", err)
		r.WallClockSeconds = time.Since(started).Seconds()
		emit(r)
		return
	}

	applied, applyDetail := applyPatch(repo)
	r.Applied = applied
	if !applied {
		r.StderrTail = truncate(applyDetail)
		r.WallClockSeconds = time.Since(started).Seconds()
		emit(r)
		return
	}

	r.Stage = "build"
	built, buildDetail := build(repo)
	r.Built = built
	if !built {
		r.StderrTail = truncate(buildDetail)
		r.WallClockSeconds = time.Since(started).Seconds()
		emit(r)
		return
	}

	r.Stage = "test"
	exitCode, out, errOut := runTests(repo)
	r.ExitCode = exitCode
	r.TestsPassed = exitCode == 0
	r.StdoutTail = truncate(out)
	r.StderrTail = truncate(errOut)
	r.WallClockSeconds = time.Since(started).Seconds()
	emit(r)
}

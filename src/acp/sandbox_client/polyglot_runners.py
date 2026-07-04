"""Phase-8 polyglot sandbox runners — Go, Rust, TypeScript.

**What "polyglot" means here (honest — A1).** These are a *language-diverse
harness with a Python verification core*, NOT per-language build/test. Each
runner's in-container entrypoint is written in a different language (Go, Rust,
TypeScript) and each independently reimplements the sandbox CONTRACT — the
sentinel-delimited JSON protocol, the apply-patch step, and the build+test
invocation — but the build+test it runs is the SAME Python gate the reference
runner uses (`python3 -m compileall` + `python3 -m pytest`), because the sample
repo's verifiable target is Python. The signal this demonstrates is "own & swap
the core": the sandbox contract is small and precise enough that three
independent language implementations produce byte-identical verdicts against it.
It does NOT claim each runner compiles and tests code in its own language — that
would need per-language fixtures and is called out as cut in DESIGN §11.
See **ADR-0003** for the framing and the parity results.

Each class conforms to the SandboxClient Protocol. The only difference from the
Python runner is the Docker image (which carries a different-language
entrypoint). The Python-side orchestration — isolation flags, patch/snapshot
mounts, sentinel parsing, wall-clock kill, the container-leak fix (unique
--name + explicit stop/rm) — is shared via the DockerSandboxRunner parent, so
each runner is a thin subclass overriding only its image default.

Contract (Phase 8 oracle):
  - Same VerificationResult shape (unchanged from Phase 3)
  - Same isolation flags (--network none, cgroups, caps-drop, non-root, ro rootfs)
  - Same sentinel-delimited JSON protocol
  - Phase-5 leak fix replicated: unique --name + _force_remove_container on all
    timeout/error exit paths (parent class owns this)
  - Parity proven by running the Phase-3 oracle suite against each runner: all
    four (Python/Go/Rust/TS) return identical verdicts on the same inputs
"""

from __future__ import annotations

from acp.sandbox_client.docker_runner import DockerSandboxRunner, SandboxLimits


class GoSandboxRunner(DockerSandboxRunner):
    """Phase-8 Go conforming runner.

    The in-container binary (sandbox/go/main.go, compiled statically via
    sandbox/go/Dockerfile) reimplements the sandbox contract in Go: apply patch →
    build gate (`python3 -m compileall`) → test (`python3 -m pytest`) → emit the
    same sentinel-delimited JSON. The build+test target is Python (the sample
    repo's verifiable core); the Go code is the harness, not the language under
    test. Parity is by shared contract + shared Python-side parser.
    """

    def __init__(
        self,
        image: str = "acp-sandbox-go:latest",
        limits: SandboxLimits | None = None,
        docker_bin: str = "docker",
    ) -> None:
        super().__init__(image=image, limits=limits, docker_bin=docker_bin)


class RustSandboxRunner(DockerSandboxRunner):
    """Phase-8 Rust conforming runner.

    The in-container binary (sandbox/rust/src/main.rs, compiled via
    sandbox/rust/Dockerfile) reimplements the sandbox contract in Rust: apply →
    build gate (`python3 -m compileall`) → test (`python3 -m pytest`) → same
    sentinel-delimited JSON. The Rust code is the harness; the verified target is
    Python (see the module docstring + ADR-0003).
    """

    def __init__(
        self,
        image: str = "acp-sandbox-rust:latest",
        limits: SandboxLimits | None = None,
        docker_bin: str = "docker",
    ) -> None:
        super().__init__(image=image, limits=limits, docker_bin=docker_bin)


class TsSandboxRunner(DockerSandboxRunner):
    """Phase-8 TypeScript/Node.js conforming runner.

    The in-container script (sandbox/ts/main.ts compiled to JS via
    sandbox/ts/Dockerfile, run by Node.js) reimplements the sandbox contract in
    TypeScript: apply → build gate (`python3 -m compileall`) → test
    (`python3 -m pytest`) → same sentinel-delimited JSON. The TS code is the
    harness; the verified target is Python (see the module docstring + ADR-0003).
    """

    def __init__(
        self,
        image: str = "acp-sandbox-ts:latest",
        limits: SandboxLimits | None = None,
        docker_bin: str = "docker",
    ) -> None:
        super().__init__(image=image, limits=limits, docker_bin=docker_bin)

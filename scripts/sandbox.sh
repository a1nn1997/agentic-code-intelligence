#!/usr/bin/env bash
# Sandbox Docker helper — the ONE place that invokes the `docker` CLI from the
# operator surface. The Makefile is a thin menu (locked decision); the actual
# docker logic (build + leak cleanup) lives here so it is real, testable shell in
# one file rather than scattered inline across Make recipes.
#
# The Python sandbox runner (src/acp/sandbox_client/docker_runner.py) still calls
# `docker run` itself — it must, to do its job — but every OPERATOR-initiated
# docker action goes through this script.
#
# Usage:
#   scripts/sandbox.sh build [IMAGE]   # build the sandbox runner image
#   scripts/sandbox.sh clean [IMAGE]   # remove leaked containers of IMAGE (never fails)
#   scripts/sandbox.sh count [IMAGE]   # print how many leaked containers exist
#
# `clean` is intentionally best-effort and ALWAYS exits 0: it runs as a trailing
# cleanup after test/verify targets (including when those fail), so it must never
# turn a green run red or mask the real exit code. If Docker is not installed or
# not running, it is a silent no-op.

set -uo pipefail

IMAGE="${2:-acp-sandbox:latest}"

_have_docker() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }

cmd_build() {
  # Build is allowed to fail loudly — a broken image must stop the pipeline.
  # IMAGE selects which runner to build:
  #   acp-sandbox:latest     -> Python reference runner (Phase 3)
  #   acp-sandbox-go:latest  -> Go runner (Phase 8)
  #   acp-sandbox-rust:latest-> Rust runner (Phase 8)
  #   acp-sandbox-ts:latest  -> TypeScript/Node.js runner (Phase 8)
  set -e
  case "$IMAGE" in
    acp-sandbox-go:*|acp-sandbox-go)
      docker build -t "$IMAGE" -f sandbox/go/Dockerfile sandbox/go
      ;;
    acp-sandbox-rust:*|acp-sandbox-rust)
      # Uses vendored dependencies (cargo vendor) — no crates.io network needed.
      docker build -t "$IMAGE" -f sandbox/rust/Dockerfile sandbox/rust
      ;;
    acp-sandbox-ts:*|acp-sandbox-ts)
      docker build -t "$IMAGE" -f sandbox/ts/Dockerfile sandbox/ts
      ;;
    *)
      # Default: Python reference runner
      docker build -t "$IMAGE" -f sandbox/Dockerfile sandbox
      ;;
  esac
}

cmd_clean() {
  # Best-effort, never fatal. Force-remove every container (running or exited)
  # whose ancestor is the sandbox image — these are the leaks the wall-clock-kill
  # path can leave behind (killing the `docker run` client before Docker reaps
  # the child can defeat `--rm`). Filtering by ancestor scopes removal to OUR
  # sandbox containers only; nothing else on the host is touched.
  if ! _have_docker; then
    echo "sandbox-clean: docker unavailable — nothing to clean"
    return 0
  fi
  local ids
  ids="$(docker ps -aq --filter "ancestor=$IMAGE" 2>/dev/null)"
  if [ -z "$ids" ]; then
    echo "sandbox-clean: no leaked $IMAGE containers"
    return 0
  fi
  local n
  n="$(printf '%s\n' "$ids" | grep -c .)"
  # shellcheck disable=SC2086
  docker rm -f $ids >/dev/null 2>&1 || true
  echo "sandbox-clean: removed $n leaked $IMAGE container(s)"
  return 0
}

cmd_count() {
  if ! _have_docker; then echo 0; return 0; fi
  local ids
  ids="$(docker ps -aq --filter "ancestor=$IMAGE" 2>/dev/null)"
  if [ -z "$ids" ]; then echo 0; else printf '%s\n' "$ids" | grep -c .; fi
}

case "${1:-}" in
  build) cmd_build ;;
  clean) cmd_clean ;;
  count) cmd_count ;;
  *)
    echo "usage: $0 {build|clean|count} [IMAGE]" >&2
    exit 2
    ;;
esac

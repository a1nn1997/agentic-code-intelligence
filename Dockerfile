# Control-plane image. Hermetic Poetry install off the committed lockfile so the
# build is deterministic ("runs first go"). Multi-stage: a builder resolves and
# installs deps into a venv, the runtime stage copies only that venv + source.

# ---- builder ----------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV POETRY_VERSION=2.4.1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install "poetry==${POETRY_VERSION}"

WORKDIR /app
# Copy only resolution inputs first, so the dep layer caches across code changes.
COPY pyproject.toml poetry.lock README.md ./
# Deps only (no project), main group only — hermetic, from the lockfile.
RUN poetry install --no-root --only main

# Now the source + every package declared in [tool.poetry].packages, then install
# the project itself into the same venv. pyproject declares TWO packages —
# `acp` (from src) and top-level `eval` — so poetry needs BOTH present or
# `poetry install` fails with "/app/eval does not contain any element".
COPY src ./src
COPY eval ./eval
RUN poetry install --only main

# ---- runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Non-root by construction.
RUN useradd --create-home --uid 10001 acp
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
# poetry installs the project via a .pth pointing at /app/src and /app (no copy
# into site-packages), so `import eval` resolves to /app/eval — it MUST be present
# in the runtime image, not just the builder, or the declared package is unimportable.
COPY --from=builder /app/eval /app/eval
# The demo runtime path (`agentctl demo-happy`) reads sample_repo as a sibling of the
# eval package: Path(/app/eval/runner.py).parent.parent/"sample_repo" == /app/sample_repo.
# Copy it so a task can actually run inside the container (56K, not test-only).
COPY sample_repo /app/sample_repo
COPY pyproject.toml README.md ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ACP_DATABASE_URL=sqlite:////app/var/acp.db

# Writable state dir owned by the non-root user.
RUN mkdir -p /app/var /app/artifacts && chown -R acp:acp /app/var /app/artifacts
USER acp

EXPOSE 8000
# Migrate (idempotent) then serve. The gateway is the only listener.
CMD ["sh", "-c", "agentctl migrate && agentctl serve"]

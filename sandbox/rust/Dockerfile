# Phase-8 Rust sandbox runner image.
# Build stage: compile the Rust entrypoint using VENDORED dependencies so no
# network access is needed inside the Docker builder — crates.io is unreachable
# inside Docker in some environments (SSL cert issues). Vendor directory was
# produced by `cargo vendor` on the host and is included in the build context.
FROM rust:1.82-slim-bookworm AS builder
WORKDIR /src
COPY Cargo.toml ./
COPY src/ ./src/
# No external dependencies — stdlib-only. No network access needed.
RUN cargo build --release 2>&1

# Final image: same base as Python runner.
FROM python:3.12-slim
RUN pip install --no-cache-dir "pytest==8.3.4"

# Non-root user baked in — same UID as the Python runner.
RUN groupadd -g 10001 sandbox && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin sandbox

COPY --from=builder /src/target/release/acp-sandbox-rust /opt/acp-sandbox-rust

# /work is tmpfs mount at runtime.
USER 10001:10001
ENTRYPOINT ["/opt/acp-sandbox-rust"]

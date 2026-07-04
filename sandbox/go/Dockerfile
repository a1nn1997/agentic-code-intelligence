# Phase-8 Go sandbox runner image.
# Build stage: compile the Go entrypoint statically so it runs without a Go
# runtime in the final image. The final image is python:3.12-slim (same base as
# the Python reference runner) so the build and test gates call the same python3/
# pytest binaries — parity by construction.
FROM golang:1.22-bookworm AS builder
WORKDIR /src
COPY go.mod ./
COPY main.go ./
# CGO_ENABLED=0 + GOOS=linux produces a fully static binary.
RUN CGO_ENABLED=0 GOOS=linux go build -o /acp-sandbox-go ./main.go

# Final image: same base as python reference runner.
FROM python:3.12-slim
RUN pip install --no-cache-dir "pytest==8.3.4"

# Non-root user baked in — same UID as the Python runner for consistent host flags.
RUN groupadd -g 10001 sandbox && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin sandbox

COPY --from=builder /acp-sandbox-go /opt/acp-sandbox-go

# /work is tmpfs mount at runtime.
USER 10001:10001
ENTRYPOINT ["/opt/acp-sandbox-go"]

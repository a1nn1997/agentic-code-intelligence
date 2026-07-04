# Phase-8 TypeScript sandbox runner image.
# Build stage: compile TS -> JS. The final image is python:3.12-slim (same base
# as the Python reference runner) so build/test gates call the same python3/pytest
# binaries — parity by construction. Node.js is installed for the runtime.
FROM node:20-slim AS builder
WORKDIR /src
COPY package.json tsconfig.json main.ts ./
RUN npm install --save-dev typescript @types/node && npx tsc

# Final image: python:3.12-slim + Node.js for running the compiled JS.
FROM python:3.12-slim
RUN pip install --no-cache-dir "pytest==8.3.4"

# Node.js runtime (minimal).
RUN apt-get update && apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/*

# Non-root user baked in — same UID as the Python runner.
RUN groupadd -g 10001 sandbox && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin sandbox

COPY --from=builder /src/dist/main.js /opt/acp-sandbox-ts.js

# /work is tmpfs mount at runtime.
USER 10001:10001
ENTRYPOINT ["node", "/opt/acp-sandbox-ts.js"]

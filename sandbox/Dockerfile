# Sandbox runner image — the isolated tier that executes UNTRUSTED code.
#
# It contains only what the apply->build->test pipeline needs: a Python
# interpreter and pytest. No git, no network tools, no shell conveniences — the
# smaller the surface, the less an adversarial patch can reach for. The image is
# started by the host runner with --network=none, --read-only, dropped caps, a
# non-root user, and a tmpfs /work; those runtime flags (not this Dockerfile)
# are what enforce isolation, and they are asserted by the Phase-3 tests.

FROM python:3.12-slim

# pytest is the repo's own test driver; pin it for deterministic runs. Installed
# at build time so the run itself needs no network (egress is denied at runtime).
RUN pip install --no-cache-dir "pytest==8.3.4"

# Non-root user baked in as a defense-in-depth default; the host also passes
# --user at run time. UID/GID chosen high to avoid colliding with host users.
RUN groupadd -g 10001 sandbox && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin sandbox

COPY entrypoint.py /opt/entrypoint.py

# The writable work area is a tmpfs mounted at run time; nothing is written to
# the image layers. Default to the entrypoint; the host overrides nothing.
USER 10001:10001
ENTRYPOINT ["python3", "/opt/entrypoint.py"]

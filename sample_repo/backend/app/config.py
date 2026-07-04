"""Application configuration for the sample users service.

PLANTED (Phase-2 redaction target): the constants below look like real secrets
embedded in source. Retrieval must redact them so they never reach a prompt.
They are fake and inert.
"""

from __future__ import annotations

# Fake secrets — must be redacted at the retrieval boundary (Phase 2).
SECRET_KEY = "sk-live-1234567890abcdefADVERSARIALdeadbeef"
STRIPE_KEY = "sk_test_51HxfakeKEYdonotusethisisaplantedsecret"

DATABASE_URL = "postgres://users_service:localdev@localhost:5432/users"
PAGE_SIZE = 50

"""API-key hashing primitives.

We store only ``(prefix, sha256(secret))`` — never the raw secret. The prefix is
a non-sensitive lookup handle; verification is a constant-time compare of the
hash, so a timing side-channel can't leak which bytes matched. A leaked database
therefore yields no usable keys. This is the isolation/security expert's
Phase-0 contribution; auth enforcement on endpoints lands in Phase 6.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from acp.common.types import new_secret

# A short, non-secret handle stored in the clear to look a key up before the
# (constant-time) hash compare. 8 chars is plenty to avoid collisions at demo scale.
_PREFIX_LEN = 8


@dataclass(frozen=True)
class IssuedKey:
    """Result of minting a key. ``token`` is shown to the user exactly ONCE and
    never persisted; only ``prefix`` + ``key_hash`` are stored."""

    token: str  # full secret: "<prefix>.<secret>" — return to caller, do not store
    prefix: str
    key_hash: str


def hash_secret(secret: str) -> str:
    """SHA-256 hex of the secret portion. Deterministic; no salt needed because
    the input is already high-entropy random (32 url-safe bytes)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def issue_api_key() -> IssuedKey:
    """Mint a new key. The full ``token`` is returned once for the caller to
    store client-side; the server keeps only prefix + hash."""
    prefix = new_secret(6)[:_PREFIX_LEN]
    secret = new_secret(32)
    token = f"{prefix}.{secret}"
    return IssuedKey(token=token, prefix=prefix, key_hash=hash_secret(secret))


def split_token(token: str) -> tuple[str, str]:
    """Split a presented ``<prefix>.<secret>`` into its parts."""
    prefix, _, secret = token.partition(".")
    return prefix, secret


def verify_secret(secret: str, stored_hash: str) -> bool:
    """Constant-time verify of a presented secret against the stored hash."""
    return hmac.compare_digest(hash_secret(secret), stored_hash)

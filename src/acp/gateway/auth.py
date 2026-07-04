"""FastAPI authentication dependency for Phase 6.

Every /v1 endpoint injects ``Depends(require_auth)`` which:
1. Reads ``Authorization: Bearer <token>`` from the request.
2. Looks up the key by prefix in the DB.
3. Constant-time verifies the secret against the stored hash.
4. Enforces the per-key rate limit: a trailing-60-second sliding window counted
   server-side (in-process, keyed by api_key_id); a burst past
   ``rate_limit_per_min`` raises 429 (A10).
5. Returns the ``(user_id, api_key_id)`` pair — this is the ONLY source of
   user_id for any read/write on the /v1 path. A client-supplied user_id in
   the request body is ignored by construction: the endpoints never accept one.

Isolation guarantee: user_id comes from the verified key row, not from the
request. A caller cannot forge identity by supplying a different user_id.

Rate-limit scope (honest): the window is held in-process, which is exactly right
for this single-process modular monolith. A horizontally-scaled deployment would
move the counter to a shared store (Redis token-bucket, or the DB row-counted
window the schema's ``rate_limit_per_min`` column anticipates); the check site
(``require_auth``) and the 429 contract stay identical — only the backing store
swaps.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request

from acp.common.errors import AuthError, RateLimitExceeded
from acp.common.security import split_token, verify_secret
from acp.db.connection import Database
from acp.db.repositories import ApiKeysRepo

_WINDOW_SECONDS = 60.0


class _SlidingWindowLimiter:
    """Thread-safe per-key trailing-window request counter.

    Keeps request timestamps per api_key_id; on each hit it drops timestamps
    older than the window, then admits iff the remaining count is under the
    key's limit. O(requests-in-window) per call, bounded by the limit itself.
    """

    def __init__(self, window_seconds: float = _WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key_id: str, limit_per_min: int, *, now: float | None = None) -> None:
        """Admit the request or raise ``RateLimitExceeded`` (429)."""
        if limit_per_min <= 0:  # 0/negative = unlimited; never throttle
            return
        t = time.monotonic() if now is None else now
        with self._lock:
            dq = self._hits[key_id]
            cutoff = t - self._window
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit_per_min:
                raise RateLimitExceeded(
                    f"rate limit exceeded: {limit_per_min} requests/min"
                )
            dq.append(t)

    def reset(self) -> None:
        """Clear all windows — used by tests for isolation."""
        with self._lock:
            self._hits.clear()


# Module-level limiter: one process, one shared window store.
_LIMITER = _SlidingWindowLimiter()


@dataclass(frozen=True)
class AuthContext:
    """Verified caller identity. user_id derives from the API key ONLY."""

    user_id: str
    api_key_id: str


def _get_db(request: Request) -> Database:
    """Extract the shared DB from app state (injected by create_app)."""
    return request.app.state.db  # type: ignore[no-any-return]


def require_auth(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    db: Database = Depends(_get_db),  # noqa: B008
) -> AuthContext:
    """FastAPI dependency: authenticate the caller; raise 401 on any failure.

    The returned AuthContext carries user_id derived from the verified key —
    never from a client-supplied header or body field.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError("missing or malformed Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    if not token or "." not in token:
        raise AuthError("token must be <prefix>.<secret>")

    prefix, secret = split_token(token)
    repo = ApiKeysRepo(db)
    key = repo.get_by_prefix(prefix)

    if key is None or not verify_secret(secret, key.key_hash):
        raise AuthError("invalid API key")

    # A10: per-key sliding-window rate limit. Checked only after the key is
    # authenticated, so an unauthenticated flood still 401s (cheaper) and a bad
    # prefix can't consume a real key's window.
    _LIMITER.check(key.id, key.rate_limit_per_min)

    return AuthContext(user_id=key.user_id, api_key_id=key.id)

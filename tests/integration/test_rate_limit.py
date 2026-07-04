"""A10 oracle: per-key rate limiting is enforced (429 on burst), not just claimed.

`auth.py` documented a per-key rate limit and `schema.sql` carries
`rate_limit_per_min`, but `require_auth` enforced nothing — a false security
claim against Hard Req #3. This suite proves the sliding window:

  * a burst past `rate_limit_per_min` within the window returns HTTP 429, and
  * the limiter admits again once the window rolls forward (unit-level, using an
    injected clock so the test is deterministic and fast).

Before the fix every request returns its normal status (never 429) → FAILS.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acp.common.errors import RateLimitExceeded
from acp.common.security import issue_api_key
from acp.config.settings import Settings
from acp.db.repositories import ApiKeysRepo, UsersRepo
from acp.gateway.app import create_app
from acp.gateway.auth import _LIMITER, _SlidingWindowLimiter
from acp.sandbox_client.stub import StubSandboxClient

pytestmark = pytest.mark.integration

_LIMIT = 3


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    """The limiter is module-global; clear it so tests don't leak windows."""
    _LIMITER.reset()


@pytest.fixture
def client_and_token(settings: Settings, tmp_path: Path):  # type: ignore[no-untyped-def]
    app = create_app(settings=settings, sandbox=StubSandboxClient())
    db = app.state.db
    user = UsersRepo(db).create()
    issued = issue_api_key()
    ApiKeysRepo(db).create(user.id, issued.prefix, issued.key_hash, rate_limit_per_min=_LIMIT)
    return TestClient(app, raise_server_exceptions=False), issued.token


def test_burst_past_rate_limit_returns_429(client_and_token) -> None:  # type: ignore[no-untyped-def]
    client, token = client_and_token
    headers = {"Authorization": f"Bearer {token}"}

    # First _LIMIT authenticated requests are admitted (404 for unknown task —
    # the point is they pass auth + rate-limit, not that the task exists).
    for _ in range(_LIMIT):
        resp = client.get("/v1/tasks/unknown", headers=headers)
        assert resp.status_code != 429, "within-limit requests must not be throttled"

    # The next one, still inside the 60s window, trips the limiter.
    resp = client.get("/v1/tasks/unknown", headers=headers)
    assert resp.status_code == 429, "a burst past rate_limit_per_min must return 429"
    assert resp.json()["error"] == "rate_limited"


def test_limiter_admits_after_window_rolls() -> None:
    """Deterministic unit check: the sliding window forgets old hits."""
    lim = _SlidingWindowLimiter(window_seconds=60.0)
    # Fill the window at t=0.
    for i in range(_LIMIT):
        lim.check("k", _LIMIT, now=0.0 + i * 0.1)
    # One more at t≈0 is rejected.
    with pytest.raises(RateLimitExceeded):
        lim.check("k", _LIMIT, now=0.5)
    # After the window fully rolls past, the key is admitted again.
    lim.check("k", _LIMIT, now=61.0)


def test_zero_limit_means_unlimited() -> None:
    lim = _SlidingWindowLimiter()
    for i in range(1000):
        lim.check("k", 0, now=float(i))  # never raises

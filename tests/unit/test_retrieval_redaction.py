"""Unit tests for the retrieval accounting + secret-redaction primitives.

These are pure-function tests (no DB, no workspace) covering the two building
blocks the RetrievalService composes: deterministic token cost and
boundary redaction against the Phase-1 planted secrets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acp.retrieval.accounting import byte_count, token_cost
from acp.retrieval.redaction import redact

pytestmark = pytest.mark.unit

_SAMPLE_REPO = Path(__file__).resolve().parents[2] / "sample_repo"

# The exact fake secret VALUES planted in Phase 1. None may ever appear in
# post-redaction output.
_PLANTED_SECRET_VALUES = [
    "sk_live_PLANTEDsecretDEADBEEF0123456789abcdef",
    "hunter2-planted-do-not-use",
    "eyJfake.PLANTED.signingkey",
    "sk-live-1234567890abcdefADVERSARIALdeadbeef",
    "sk_test_51HxfakeKEYdonotusethisisaplantedsecret",
]


def test_token_cost_is_monotonic_in_bytes() -> None:
    """More returned bytes never costs fewer tokens — the property that makes a
    span provably cheaper than a whole file."""
    assert token_cost("x" * 40) < token_cost("x" * 400)
    assert token_cost("") <= token_cost("a")


def test_token_cost_is_deterministic() -> None:
    """Same content ⇒ identical cost across calls (replay determinism)."""
    content = "def f():\n    return 1\n"
    assert token_cost(content) == token_cost(content)
    assert byte_count(content) == byte_count(content)


def test_redacts_env_planted_secrets() -> None:
    text = (_SAMPLE_REPO / ".env").read_text(encoding="utf-8")
    result = redact(text)
    assert result.redacted
    for value in _PLANTED_SECRET_VALUES:
        if value in text:
            assert value not in result.content


def test_redacts_config_planted_secrets() -> None:
    text = (_SAMPLE_REPO / "backend/app/config.py").read_text(encoding="utf-8")
    result = redact(text)
    assert result.redacted
    for value in _PLANTED_SECRET_VALUES:
        if value in text:
            assert value not in result.content
    # Structure preserved, benign config untouched.
    assert "SECRET_KEY" in result.content
    assert "postgres://users_service" in result.content  # DATABASE_URL allow-listed


def test_redaction_is_idempotent() -> None:
    """Re-redacting redacted text yields identical bytes — so re-reading a span
    replays identically."""
    text = (_SAMPLE_REPO / "backend/app/config.py").read_text(encoding="utf-8")
    once = redact(text).content
    twice = redact(once).content
    assert once == twice


def test_redaction_events_report_count_not_value() -> None:
    text = (_SAMPLE_REPO / ".env").read_text(encoding="utf-8")
    result = redact(text)
    assert result.total_redactions >= 3
    # Events carry only reason + count — never the secret value.
    for event in result.events:
        for value in _PLANTED_SECRET_VALUES:
            assert value not in event.reason

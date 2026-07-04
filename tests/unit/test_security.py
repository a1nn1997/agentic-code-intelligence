"""API-key hashing: round-trip, constant-time verify, no raw secret retained."""

from __future__ import annotations

import pytest

from acp.common.security import (
    hash_secret,
    issue_api_key,
    split_token,
    verify_secret,
)

pytestmark = pytest.mark.unit


def test_issue_key_round_trip() -> None:
    issued = issue_api_key()
    prefix, secret = split_token(issued.token)
    assert prefix == issued.prefix
    assert verify_secret(secret, issued.key_hash)


def test_stored_hash_is_not_the_secret() -> None:
    issued = issue_api_key()
    _, secret = split_token(issued.token)
    # The stored hash must not equal or contain the raw secret.
    assert issued.key_hash != secret
    assert secret not in issued.key_hash
    assert issued.key_hash == hash_secret(secret)


def test_wrong_secret_fails_verify() -> None:
    issued = issue_api_key()
    assert not verify_secret("not-the-secret", issued.key_hash)


def test_hash_is_deterministic() -> None:
    assert hash_secret("abc") == hash_secret("abc")
    assert hash_secret("abc") != hash_secret("abd")

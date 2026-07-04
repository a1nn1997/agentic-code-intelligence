"""A11 oracle: multi-file rename is collision-safe (identifier tokens, not substrings).

The previous rename used ``original.replace(old_name, new_name)`` — a raw
whole-file substitution that corrupts every colliding identifier: renaming
``user`` would rewrite ``user_id`` → ``new_id``, ``username`` → ``newname``, and
``get_user`` → ``get_new``. The fix replaces only ``\\b``-anchored whole
identifier tokens.

These unit tests pin the token semantics directly (fast, deterministic). The
integration proof that the loop uses this is in test_multifile_rename.py (which
renames a real symbol across files without corrupting collisions).
"""

from __future__ import annotations

import pytest

from acp.orchestrator.loop import _rename_identifier_tokens

pytestmark = pytest.mark.unit


def test_rename_does_not_corrupt_colliding_identifiers() -> None:
    src = (
        "def get_user(user_id):\n"
        "    user = lookup(user_id)\n"
        "    username = user.name\n"
        "    return user\n"
    )
    out = _rename_identifier_tokens(src, "user", "account")

    # The standalone token `user` IS renamed.
    assert "account = lookup(user_id)" in out
    assert "username = account.name" in out
    assert "return account" in out

    # Colliding identifiers are UNTOUCHED (the whole-file str.replace bug).
    assert "user_id" in out and "new_id" not in out
    assert "username" in out  # not "accountname"
    assert "get_user" in out  # not "get_account"
    # And no stray corruption of the collisions.
    assert "account_id" not in out
    assert "accountname" not in out


def test_rename_hits_every_standalone_occurrence() -> None:
    src = "x = user\ny = user + user\nz = useruser\n"
    out = _rename_identifier_tokens(src, "user", "u2")
    assert out == "x = u2\ny = u2 + u2\nz = useruser\n"  # `useruser` is one token → untouched


def test_rename_respects_dotted_boundaries() -> None:
    # `user` as an attribute name is a whole token; `.user_id` is not.
    src = "a = obj.user\nb = obj.user_id\n"
    out = _rename_identifier_tokens(src, "user", "acct")
    assert out == "a = obj.acct\nb = obj.user_id\n"

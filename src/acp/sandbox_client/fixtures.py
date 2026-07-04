"""Deterministic, model-free verification fixtures for the Phase-3 oracle.

Each fixture returns a patch envelope (the owned JSON format the sandbox
applies) against the Phase-1 sample repo. Because they are fixed patches — no
model in the loop — the oracle is fully reproducible: the same patch always
produces the same VerificationResult. Every clause of the Phase-3 oracle has a
fixture here that exercises the REAL mechanism, not a proxy.

    fixture              | proves
    ---------------------|--------------------------------------------------
    good_patch           | applied + built + tests_passed, exit 0
    bad_patch            | tests_passed=false with the REAL error text present
    network_patch        | a socket connect fails closed under --network=none
    infinite_loop_patch  | a spinning test is killed at the wall-clock deadline
    oom_patch            | an over-limit allocation is OOM-killed

The bad patch embeds a unique, greppable marker string in the failing assertion
so a test can assert the actual captured failure text is returned (this is what
Phase-5 self-repair reads), not merely a generic ``tests_passed=false`` flag.
"""

from __future__ import annotations

import json

# Unique sentinel baked into the bad patch's failing assertion. The oracle
# asserts THIS string round-trips through the sandbox in stderr/stdout, proving
# real failure output is captured rather than fabricated.
BAD_PATCH_MARKER = "ACP_KNOWN_BAD_ASSERTION_a1b2c3"


def _envelope(*ops: dict[str, object]) -> str:
    return json.dumps({"ops": list(ops)})


def good_patch() -> str:
    """Adds a new, self-contained passing test. Builds and passes -> exit 0."""
    content = '''"""Known-good verification fixture: a passing test the sandbox must green."""

from __future__ import annotations

from app.users.service import serialize_user
from app.users.models import User


def test_known_good_serialize() -> None:
    user = User(id="g1", name="Grace Hopper", email="grace@example.com")
    result = serialize_user(user)
    assert result["id"] == "g1"
    assert result["active"] is True
'''
    return _envelope(
        {"op": "write", "path": "backend/tests/test_known_good.py", "content": content}
    )


def bad_patch() -> str:
    """Adds a test that fails with a UNIQUE, identifiable assertion message.

    The failure text (BAD_PATCH_MARKER) must be present in the returned output,
    proving real captured stderr/stdout — the input Phase-5 self-repair reads.
    """
    content = f'''"""Known-bad verification fixture: a test that must FAIL with a known marker."""

from __future__ import annotations

from app.users.service import serialize_user
from app.users.models import User


def test_known_bad_serialize() -> None:
    user = User(id="b1", name="Katherine Johnson", email="kj@example.com")
    result = serialize_user(user)
    # Deliberately wrong expectation carrying a unique marker string.
    assert result["id"] == "WRONG", "{BAD_PATCH_MARKER}"
'''
    return _envelope(
        {"op": "write", "path": "backend/tests/test_known_bad.py", "content": content}
    )


def network_patch() -> str:
    """A test that attempts real egress. Under --network=none it must fail
    closed (never succeed), and the runner classifies it as an egress kill."""
    content = '''"""Egress fixture: attempts a network call that MUST fail closed."""

from __future__ import annotations

import socket


def test_network_is_denied() -> None:
    # A real connect attempt. With --network=none this raises OSError
    # (Network is unreachable / name resolution failure), never succeeds.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(("8.8.8.8", 53))  # should be unreachable in the sandbox
    sock.close()
'''
    return _envelope(
        {"op": "write", "path": "backend/tests/test_network.py", "content": content}
    )


def infinite_loop_patch() -> str:
    """A test that never returns. Must be killed at the wall-clock deadline and
    reported as a timeout — the caller must not hang or get a false pass."""
    content = '''"""Wall-clock fixture: a test that runs forever and must be killed."""

from __future__ import annotations


def test_runs_forever() -> None:
    while True:
        pass
'''
    return _envelope(
        {"op": "write", "path": "backend/tests/test_infinite.py", "content": content}
    )


def oom_patch() -> str:
    """A test that allocates past the memory limit. With swap disabled and a
    memory cgroup, the container is OOM-killed (docker rc 137)."""
    content = '''"""Memory-limit fixture: allocate past the cgroup cap to force an OOM kill."""

from __future__ import annotations


def test_allocates_too_much() -> None:
    blocks = []
    # Each block ~100MB; the sandbox memory cap is far below the total, so the
    # kernel OOM-kills the process before this completes.
    while True:
        blocks.append(bytearray(100 * 1024 * 1024))
'''
    return _envelope(
        {"op": "write", "path": "backend/tests/test_oom.py", "content": content}
    )


def bounded_alloc_patch() -> str:
    """Allocates a FIXED ~200MB, then asserts success. This proves the memory
    limit *binds*: it PASSES under a >200MB cap but is OOM-killed under a <200MB
    cap. Contrasting the two runs shows the cgroup limit is what decides, not the
    workload — the limit is demonstrably taking effect, not incidentally."""
    content = '''"""Bounded-allocation fixture: ~200MB, passes iff the memory cap allows it."""

from __future__ import annotations


def test_allocates_200mb() -> None:
    block = bytearray(200 * 1024 * 1024)
    # Touch pages so the allocation is resident, not lazy.
    for i in range(0, len(block), 4096):
        block[i] = 1
    assert len(block) == 200 * 1024 * 1024
'''
    return _envelope(
        {"op": "write", "path": "backend/tests/test_bounded_alloc.py", "content": content}
    )


def unapplyable_patch() -> str:
    """A patch whose path escapes the work dir. Must fail at APPLY (applied=false)
    with the traversal reported — proving the apply gate is real and fails closed."""
    return _envelope({"op": "write", "path": "../evil.py", "content": "x = 1\n"})


def unbuildable_patch() -> str:
    """A patch that writes syntactically invalid Python. Must fail at BUILD
    (built=false) with the REAL SyntaxError text — proving the build gate is real."""
    return _envelope(
        {"op": "write", "path": "backend/app/broken.py", "content": "def (:\n"}
    )


def empty_patch() -> str:
    """No-op patch: verifies the repo's own suite passes unmodified (baseline)."""
    return _envelope()

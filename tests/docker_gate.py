"""Shared Docker-gate for the sandbox integration tests (A8).

The real proof of the assignment's central claim — "a change is done only after
a real build + test in the sandbox" — lives in the Docker-gated tests. Silently
*skipping* them when the image is merely un-built hollows that proof: a green run
can hide the fact that the real path never executed.

This gate distinguishes three states per required image:

  1. Image present            → RUN the test.
  2. Image absent, Docker up,
     and ``ACP_REQUIRE_DOCKER=1`` (set by ``make eval-docker`` / ``make
     test-docker``)            → FAIL loudly. The image is *buildable* and the
                                  operator asked for the real proof; a skip here
                                  would be a false green.
  3. Otherwise (Docker daemon
     unreachable, or no explicit
     requirement)              → SKIP with a reason (a laptop without Docker is a
                                  legitimate reason to defer the real path).

Use ``requires_sandbox("acp-sandbox:latest")`` as a decorator marker.
"""

from __future__ import annotations

import functools
import os
import subprocess
from collections.abc import Callable
from typing import Any, TypeVar

import pytest

_REQUIRE_ENV = "ACP_REQUIRE_DOCKER"

_F = TypeVar("_F", bound=Callable[..., Any])


def _image_present(image: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, timeout=15
        )
        return r.returncode == 0
    except Exception:
        return False


def _docker_daemon_up() -> bool:
    """True if the Docker daemon answers — i.e. the image is *buildable* here."""
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


def docker_required() -> bool:
    """Whether the operator demanded the real Docker path (make eval-docker)."""
    return os.environ.get(_REQUIRE_ENV, "").strip().lower() in {"1", "true", "yes"}


def requires_sandbox(image: str) -> Callable[[_F], _F]:
    """Decorator enforcing the three-state gate above for ``image``.

    The gate is evaluated at *call* time (not import time) so a `make
    sandbox-build` earlier in the same ``make`` invocation is seen.
    """

    def decorator(func: _F) -> _F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _image_present(image):
                return func(*args, **kwargs)
            if docker_required():
                if _docker_daemon_up():
                    # Buildable + demanded but the image is missing → FAIL loud.
                    pytest.fail(
                        f"Docker image {image!r} is absent but {_REQUIRE_ENV}=1 "
                        f"and the Docker daemon is up — the image is buildable and "
                        f"the real-sandbox proof was demanded. Build it with "
                        f"`make sandbox-build` (or the polyglot equivalent). "
                        f"Refusing to report green without the real build+test path.",
                        pytrace=False,
                    )
                pytest.fail(
                    f"{_REQUIRE_ENV}=1 but the Docker daemon is unreachable — "
                    f"cannot run the required real-sandbox proof for {image!r}.",
                    pytrace=False,
                )
            pytest.skip(
                f"Docker image {image!r} absent and not required "
                f"(set {_REQUIRE_ENV}=1 with Docker up to force the real proof; "
                f"build via `make sandbox-build`)"
            )
            return None

        return wrapper  # type: ignore[return-value]

    return decorator

"""Config layer: keyless-by-default, fail-closed key handling, path parsing."""

from __future__ import annotations

import pytest

from acp.common.errors import ConfigError
from acp.config.settings import ModelBackend, SandboxRunner, Settings

pytestmark = pytest.mark.unit


def test_defaults_are_keyless_stub_mode() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.model_backend == ModelBackend.STUB
    assert s.model_api_key.get_secret_value() == ""
    assert s.sandbox_runner == SandboxRunner.PYTHON
    # Budget defaults are present and sane.
    assert s.default_task_token_budget > 0
    assert s.default_task_step_budget >= 1


def test_claude_backend_without_key_fails_closed() -> None:
    with pytest.raises(ConfigError):
        Settings(_env_file=None, model_backend="claude", model_api_key="")  # type: ignore[call-arg]


def test_claude_backend_with_key_ok() -> None:
    s = Settings(_env_file=None, model_backend="claude", model_api_key="secret")  # type: ignore[call-arg]
    assert s.require_model_key() == "secret"


def test_require_model_key_raises_when_unset() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ConfigError):
        s.require_model_key()


def test_sqlite_path_extracted_from_url() -> None:
    s = Settings(_env_file=None, database_url="sqlite:///./var/x.db")  # type: ignore[call-arg]
    assert s.sqlite_path == "./var/x.db"


def test_non_sqlite_url_rejected_in_phase0() -> None:
    s = Settings(_env_file=None, database_url="postgresql://localhost/x")  # type: ignore[call-arg]
    with pytest.raises(ConfigError):
        _ = s.sqlite_path

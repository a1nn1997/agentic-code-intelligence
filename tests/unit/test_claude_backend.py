"""B-1 + B-2 oracle: the Claude backend maps SDK failures and stop_reasons to
typed errors — never a raw SDK exception, never a misleading generic parse error,
never a leaked secret.

The Anthropic SDK is NOT a core dependency (the keyless eval never imports it),
so these tests install a **fake** ``anthropic`` module into ``sys.modules`` with
the four exception classes the backend maps and a scriptable fake client. That
lets us drive ``ClaudeModelBackend.complete`` down its real error/branch paths
without a key or a network.

B-1: each of RateLimitError / APIStatusError / APIConnectionError /
AuthenticationError raised by ``messages.create`` becomes a typed
:class:`UpstreamModelError` carrying ``request_id`` and NO secret.

B-2: ``stop_reason`` "refusal" → :class:`ModelRefusal`; "max_tokens" →
:class:`ModelTruncated`; only "end_turn" assembles a normal ModelResponse.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from acp.common.errors import UpstreamModelError
from acp.config.settings import Settings
from acp.model_gateway.interface import ModelRequest
from acp.model_gateway.prompt import ModelRefusal, ModelTruncated

pytestmark = pytest.mark.unit

_SECRET_KEY = "sk-ant-SUPERSECRET-should-never-appear"


# --- fake anthropic SDK ------------------------------------------------------
class _FakeAPIError(Exception):
    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


class _FakeRateLimitError(_FakeAPIError):
    pass


class _FakeAuthenticationError(_FakeAPIError):
    pass


class _FakeAPIConnectionError(_FakeAPIError):
    pass


class _FakeAPIStatusError(_FakeAPIError):
    def __init__(self, message: str, *, status_code: int, request_id: str | None = None) -> None:
        super().__init__(message, request_id=request_id)
        self.status_code = status_code


class _FakeMessages:
    def __init__(self, on_create: Any) -> None:
        self._on_create = on_create

    def create(self, **kwargs: Any) -> Any:
        return self._on_create(**kwargs)


class _FakeAnthropicClient:
    def __init__(self, api_key: str, on_create: Any) -> None:
        self.api_key = api_key
        self.messages = _FakeMessages(on_create)


def _install_fake_anthropic(on_create: Any) -> types.ModuleType:
    mod = types.ModuleType("anthropic")
    mod.RateLimitError = _FakeRateLimitError  # type: ignore[attr-defined]
    mod.AuthenticationError = _FakeAuthenticationError  # type: ignore[attr-defined]
    mod.APIConnectionError = _FakeAPIConnectionError  # type: ignore[attr-defined]
    mod.APIStatusError = _FakeAPIStatusError  # type: ignore[attr-defined]
    mod.Anthropic = lambda api_key: _FakeAnthropicClient(api_key, on_create)  # type: ignore[attr-defined]
    return mod


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Install a fake ``anthropic`` module; each test sets ``.on_create``."""
    holder: dict[str, Any] = {"on_create": None}

    def dispatch(**kwargs: Any) -> Any:
        return holder["on_create"](**kwargs)

    mod = _install_fake_anthropic(dispatch)
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    yield holder
    # monkeypatch.setitem restores sys.modules automatically.


def _backend() -> Any:
    from acp.model_gateway.claude import ClaudeModelBackend

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        model_backend="claude",
        model_api_key=_SECRET_KEY,
        env="test",
    )
    return ClaudeModelBackend(settings)


def _req() -> ModelRequest:
    return ModelRequest(
        task_id="t",
        step_index=1,
        instruction_xml="<instruction>fix</instruction>",
        data_xml="<data/>",
    )


class _Msg:
    def __init__(self, stop_reason: str, text: str = "") -> None:
        self.stop_reason = stop_reason
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.usage = types.SimpleNamespace(input_tokens=10, output_tokens=20)


# --- B-1: SDK error mapping --------------------------------------------------
@pytest.mark.parametrize(
    "exc",
    [
        _FakeRateLimitError("rate limited", request_id="req_rl"),
        _FakeAuthenticationError("bad key", request_id="req_auth"),
        _FakeAPIConnectionError("conn reset", request_id="req_conn"),
        _FakeAPIStatusError("server error", status_code=529, request_id="req_stat"),
    ],
)
def test_sdk_errors_map_to_typed_upstream_error(fake_sdk: Any, exc: Exception) -> None:
    def raiser(**kwargs: Any) -> Any:
        raise exc

    fake_sdk["on_create"] = raiser
    with pytest.raises(UpstreamModelError) as ei:
        _backend().complete(_req())
    err = ei.value
    # request_id is carried for correlation…
    assert err.request_id == getattr(exc, "request_id", None)
    # …and NO secret / key content ever leaks into the message or code.
    assert _SECRET_KEY not in str(err)
    assert _SECRET_KEY not in err.code
    assert err.http_status == 502


def test_unknown_sdk_error_is_still_wrapped(fake_sdk: Any) -> None:
    """A provider exception outside the four known types is still typed —
    the loop must never see a raw SDK/transport exception."""

    def raiser(**kwargs: Any) -> Any:
        raise RuntimeError("something weird from the SDK")

    fake_sdk["on_create"] = raiser
    with pytest.raises(UpstreamModelError):
        _backend().complete(_req())


# --- B-2: stop_reason inspection --------------------------------------------
def test_refusal_stop_reason_raises_model_refusal(fake_sdk: Any) -> None:
    fake_sdk["on_create"] = lambda **k: _Msg("refusal", text="")
    with pytest.raises(ModelRefusal):
        _backend().complete(_req())


def test_max_tokens_stop_reason_raises_model_truncated(fake_sdk: Any) -> None:
    fake_sdk["on_create"] = lambda **k: _Msg("max_tokens", text="<action kind=\"pl")
    with pytest.raises(ModelTruncated):
        _backend().complete(_req())


def test_end_turn_assembles_normal_response(fake_sdk: Any) -> None:
    fake_sdk["on_create"] = lambda **k: _Msg("end_turn", text="<action kind=\"verify\"/>")
    resp = _backend().complete(_req())
    assert resp.content_xml == '<action kind="verify"/>'
    assert (resp.input_tokens, resp.output_tokens) == (10, 20)
    assert resp.backend == "claude"

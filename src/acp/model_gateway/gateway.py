"""The Model Gateway facade — the ONE place the API key may live.

The loop never talks to a backend directly; it calls :class:`ModelGateway`,
which owns backend selection and (for the Claude backend) the API key. The key
is read from settings' single choke point (:meth:`Settings.require_model_key`)
and is never returned, logged, or passed to a client/sandbox path. In stub mode
no key is read at all — the eval runs keyless.

The gateway does not journal or charge; that is the loop's job (so the journal
has a single owner). The gateway's contract is narrow: given separated XML
channels, return an XML reply plus token usage.
"""

from __future__ import annotations

from acp.common.logging import get_logger
from acp.config import Settings, get_settings
from acp.config.settings import ModelBackend as BackendChoice
from acp.model_gateway.interface import ModelBackend, ModelRequest, ModelResponse
from acp.model_gateway.stub import StubModelBackend

_log = get_logger(__name__)


class ModelGateway:
    """Selects and drives the configured model backend behind one interface."""

    def __init__(self, backend: ModelBackend) -> None:
        self._backend = backend

    @property
    def backend_name(self) -> str:
        return self._backend.name

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Run one model turn. The reply is strict XML; usage meters the call.

        We log only non-secret metadata (backend, task, step, token counts) —
        never the channels' contents, which could carry retrieved code."""
        resp = self._backend.complete(request)
        _log.info(
            "model.turn",
            extra={
                "backend": resp.backend,
                "task_id": request.task_id,
                "step_index": request.step_index,
                "input_tokens": resp.input_tokens,
                "output_tokens": resp.output_tokens,
            },
        )
        return resp


def build_model_gateway(settings: Settings | None = None) -> ModelGateway:
    """Construct the gateway for the configured backend.

    ``stub`` (default) is keyless and deterministic. ``claude`` constructs the
    real backend, which reads the key ONLY via ``settings.require_model_key()``
    — the single audited choke point. Import of the Claude backend is lazy so
    the stub path never imports the SDK and stays keyless/offline.
    """
    s = settings or get_settings()
    if s.model_backend == BackendChoice.STUB:
        return ModelGateway(StubModelBackend())
    if s.model_backend == BackendChoice.CLAUDE:
        from acp.model_gateway.claude import ClaudeModelBackend

        return ModelGateway(ClaudeModelBackend(s))
    raise ValueError(f"unknown model backend: {s.model_backend}")  # pragma: no cover

"""Model Gateway: the ONLY component that may hold and use the model API key.

Callers speak strict XML in and get strict XML out; the key lives here and never
in a client, a prompt, a log, or a sandbox path. ``MODEL_BACKEND`` selects the
backend: ``stub`` (deterministic, keyless — the default, makes eval run without
secrets) or ``claude`` (Anthropic API, needs a key in the gateway's env only).
Phase 0 defines the interface and the backend-selection seam.
"""

from acp.model_gateway.gateway import ModelGateway, build_model_gateway
from acp.model_gateway.interface import ModelBackend, ModelRequest, ModelResponse
from acp.model_gateway.prompt import (
    ActionKind,
    AgentAction,
    DataSpan,
    ModelProtocolError,
    ModelRefusal,
    ModelTruncated,
    build_channels,
    parse_action,
    render_prompt,
)
from acp.model_gateway.stub import StubModelBackend

__all__ = [
    "ModelBackend",
    "ModelRequest",
    "ModelResponse",
    "StubModelBackend",
    "ModelGateway",
    "build_model_gateway",
    "ActionKind",
    "AgentAction",
    "DataSpan",
    "ModelProtocolError",
    "ModelRefusal",
    "ModelTruncated",
    "build_channels",
    "parse_action",
    "render_prompt",
]

"""Interface contract for the model backend.

The request/response types enforce the trust boundary structurally: the prompt
is assembled from *labeled channels* — a trusted instruction channel (the
authenticated user's task) and an untrusted data channel (retrieved code) — so
retrieved content is never concatenated into instructions as free prose. Parses
are strict XML. Responses carry token usage so the loop can meter every call.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ModelRequest(BaseModel):
    """A single model turn, expressed as separated trust channels."""

    task_id: str
    step_index: int = Field(description="Journalled step; also the replay/idempotency key")
    instruction_xml: str = Field(description="Trusted channel: the authenticated user's intent")
    data_xml: str = Field(
        default="",
        description="Untrusted channel: retrieved code, XML-escaped; never executed as commands",
    )
    max_tokens: int = 4096


class ModelResponse(BaseModel):
    """A model turn's result plus the usage needed to meter it."""

    content_xml: str
    input_tokens: int
    output_tokens: int
    backend: str = Field(description="stub | claude")
    cached: bool = Field(default=False, description="True when served from journal replay")


@runtime_checkable
class ModelBackend(Protocol):
    """A pluggable model backend behind the gateway. Implementations must never
    surface the API key or echo secrets from the data channel."""

    name: str

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Run one model turn from the labeled channels; return XML + usage."""
        ...

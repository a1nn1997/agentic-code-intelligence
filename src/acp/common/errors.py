"""Typed error hierarchy for the control plane.

A single base (:class:`ACPError`) lets the gateway map any internal failure to a
structured HTTP response without leaking stack traces or secrets. Each subclass
carries a stable ``code`` string that appears in JSON logs and API error bodies,
so operators can grep failures by class rather than by message text.
"""

from __future__ import annotations


class ACPError(Exception):
    """Base for all deliberate control-plane errors.

    ``code`` is a stable, machine-grep-able identifier; ``message`` is the
    human-facing summary. Never put secrets or raw retrieved code in either —
    both cross the API boundary.
    """

    code: str = "acp_error"
    http_status: int = 500

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class NotImplementedInPhase(ACPError):
    """Raised by Phase-0 stubs.

    Existing so an accidental call to an unimplemented module fails loudly and
    is trivially greppable, rather than returning a plausible-but-wrong value.
    """

    code = "not_implemented_in_phase"
    http_status = 501


class ConfigError(ACPError):
    """Invalid or missing configuration discovered at startup."""

    code = "config_error"
    http_status = 500


class AuthError(ACPError):
    """Authentication or authorization failure (bad/expired/missing key)."""

    code = "auth_error"
    http_status = 401


class IsolationViolation(ACPError):
    """A read/write attempted to address data outside the caller's scope.

    This is a defense-in-depth tripwire: the scoped accessor should make
    cross-user addressing *unrepresentable*; if this is ever raised, a
    constructed-by-safety invariant was bypassed and it must be treated as a
    security incident, not a routine error.
    """

    code = "isolation_violation"
    http_status = 403


class BudgetExceeded(ACPError):
    """A server-side budget (token / wall-clock / cost) would be breached."""

    code = "budget_exceeded"
    http_status = 402


class NotFound(ACPError):
    """A referenced entity (task, workspace, artifact) does not exist in scope."""

    code = "not_found"
    http_status = 404


class ConflictError(ACPError):
    """A concurrent-write conflict was detected and rejected.

    Raised by :meth:`WorkspaceServiceImpl.commit_worktree` when the workspace
    head advanced since the worktree was opened (another task committed first).
    The caller must rebase-and-reverify or give up; there is no silent clobber.
    """

    code = "conflict"
    http_status = 409


class UpstreamModelError(ACPError):
    """A call to the upstream model provider (Anthropic API) failed.

    Raised by the Claude backend (B-1) when the SDK surfaces a transport- or
    provider-level failure — rate limit, non-2xx status, connection error, or
    authentication error. Previously a *raw* SDK exception propagated out of
    :meth:`ClaudeModelBackend.complete` and aborted the run **before the journal
    row for the step was written**, so a resume had to re-issue (and re-pay for)
    the same model call. Mapping to this typed error lets the loop journal the
    step as a recoverable failure and resume without a paid re-issue.

    ``request_id`` (the provider's request identifier, when the SDK exposes one)
    is carried for support correlation. **Neither the API key nor any data-channel
    content is ever placed in the message or on this object** — both cross the API
    boundary. HTTP 502: the control plane is healthy; its upstream failed.
    """

    code = "upstream_model_error"
    http_status = 502

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.request_id = request_id


class RateLimitExceeded(ACPError):
    """A per-key request rate limit was exceeded (sliding window).

    Raised by ``require_auth`` when a key issues more than its
    ``rate_limit_per_min`` requests within the trailing 60-second window. Maps to
    HTTP 429 so callers can back off; the window is counted server-side so a
    client cannot evade it. Enforces Hard Req #3 (auth + per-key rate limit).
    """

    code = "rate_limited"
    http_status = 429

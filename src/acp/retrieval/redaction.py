"""Secret redaction at the retrieval boundary.

This is the **first** layer of the secret-hygiene story (the sandbox egress-deny
backstop in Phase 3 is the last). It runs *inside* the retrieval layer, on the
content of every primitive, **before** that content crosses the module boundary
— so a secret value physically cannot reach a prompt, a model call, an API
response, or a log downstream. Redacting here (not at the gateway) means there
is exactly one choke point and no code path that returns raw retrieved bytes.

The patterns target two things:

1. **The Phase-1 planted secrets** — the fake keys in ``sample_repo/.env`` and
   ``sample_repo/backend/app/config.py`` (``sk_live_…``, ``sk_test_…``,
   ``sk-live-…`` provider-style keys; ``KEY``/``SECRET``/``PASSWORD``/``TOKEN``
   assignment RHS values).
2. **A defensible general set** — assignments/JSON/YAML/env whose *name* looks
   secret-bearing (``secret``, ``password``, ``token``, ``api[_-]?key``,
   ``signing_key``, etc.), and common high-entropy provider key shapes.

Redaction preserves structure: the identifier/assignment stays (so the index and
the model still see *that* there is a secret), only the **value** is replaced
with a ``«redacted:reason»`` marker. That keeps retrieved code syntactically
legible while guaranteeing the value never leaves.

Every redaction is reported as a structured :class:`RedactionEvent` so the
caller can meter it (a ``retrieval.secret_redacted`` log line + a ledger note)
without ever logging the secret value itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

REDACTION_MARKER = "«redacted:{reason}»"


@dataclass(frozen=True)
class RedactionEvent:
    """One redaction that occurred, for logging/metering. Never carries the
    secret value — only *that* one was removed, and why."""

    reason: str
    count: int


@dataclass
class RedactionResult:
    """Post-redaction content plus the events describing what was removed."""

    content: str
    events: list[RedactionEvent] = field(default_factory=list)

    @property
    def redacted(self) -> bool:
        return bool(self.events)

    @property
    def total_redactions(self) -> int:
        return sum(e.count for e in self.events)


# A rule is (reason, compiled regex). Each regex must expose a capture group
# named ``secret`` — only that group is replaced, so surrounding structure
# (the key name, quotes, assignment operator) is preserved. Order matters:
# more specific provider-key shapes run before generic name-based rules so a
# value is attributed to the most precise reason.
_SECRET_VALUE = r"(?P<secret>[^\s'\"`]+)"


def _rule(reason: str, pattern: str, flags: int = re.MULTILINE) -> tuple[str, re.Pattern[str]]:
    return reason, re.compile(pattern, flags)


_RULES: list[tuple[str, re.Pattern[str]]] = [
    # Provider-style keys by *shape*, anywhere (catches the planted sk_live_…,
    # sk_test_…, sk-live-… values even when not on an assignment line).
    _rule("provider_api_key", r"(?P<secret>sk[-_](?:live|test)?[-_]?[A-Za-z0-9]{8,})"),
    # JWT-ish three-segment tokens (the planted JWT_SIGNING_KEY value shape).
    _rule("jwt", r"(?P<secret>ey[A-Za-z0-9_-]*\.[A-Za-z0-9_.-]+\.[A-Za-z0-9_-]+)"),
    # Name-based: an identifier that looks secret-bearing = a quoted/bare value.
    # Covers Python/TS assignments (`SECRET_KEY = "..."`), env files
    # (`API_SECRET=...`), and JSON/YAML (`"password": "..."`).
    _rule(
        "named_secret",
        r"(?i)(?P<name>[A-Za-z0-9_]*"
        r"(?:secret|password|passwd|api[_-]?key|token|signing[_-]?key|"
        r"access[_-]?key|private[_-]?key|client[_-]?secret)[A-Za-z0-9_]*)"
        r"(?P<sep>\s*[:=]\s*)"
        r"(?P<q>['\"]?)" + _SECRET_VALUE + r"(?P=q)",
    ),
]

# Names that look secret-bearing but whose value is not a secret (avoid nuking
# innocuous config like DATABASE_URL). Kept tiny and explicit.
_ALLOW_NAMES = re.compile(r"(?i)^(database_url|db_url|redis_url)$")


def _marker(reason: str) -> str:
    return REDACTION_MARKER.format(reason=reason)


def redact(content: str) -> RedactionResult:
    """Scrub secrets from ``content``. Returns post-redaction text + events.

    Idempotent-ish: running it twice yields the same text (markers contain no
    characters the value-rules match), so re-reading a span replays identically.
    """
    events: list[RedactionEvent] = []
    text = content

    for reason, pattern in _RULES:
        count = 0

        def _sub(m: re.Match[str], _reason: str = reason) -> str:
            nonlocal count
            name = m.groupdict().get("name")
            if name is not None and _ALLOW_NAMES.match(name.strip()):
                return m.group(0)  # allow-listed benign name → leave untouched
            count += 1
            # Preserve any named structural groups (name/sep/quote); replace the
            # secret value only, so the line stays legible and re-parseable.
            gd = m.groupdict()
            if "name" in gd and gd["name"] is not None:
                q = gd.get("q") or ""
                return f"{gd['name']}{gd['sep']}{q}{_marker(_reason)}{q}"
            return _marker(_reason)

        text = pattern.sub(_sub, text)
        if count:
            events.append(RedactionEvent(reason=reason, count=count))

    return RedactionResult(content=text, events=events)


def redact_secrets(content: str) -> str:
    """Convenience wrapper returning only the scrubbed text.

    Reused at the **sandbox result boundary** (Phase 3): captured build/test
    output is untrusted and could echo a secret from the repo, so every
    stdout/stderr tail is passed through the same rules before it can reach a
    result, a log, or the repair loop. Same choke point, second layer.
    """
    return redact(content).content

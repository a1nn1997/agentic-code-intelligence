"""Structured JSON logging with correlation context.

Every log line is one JSON object. A :class:`contextvars.ContextVar`-backed
binding carries ``task_id`` / ``step`` / ``module`` through the call stack so
logs from any module can be correlated to a single run without threading a
logger object through every function. The journal is the durable per-run trace;
these logs are the live operational view (Phase 0 requirement #9: operable).
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# Correlation fields bound for the current async/thread context. Empty by
# default so library code that logs outside a task still produces valid lines.
_log_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "acp_log_context", default={}
)

# Standard LogRecord attributes we must not re-emit as "extra" fields.
_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object.

    Merges, in increasing precedence: the correlation context, any ``extra=``
    fields passed to the logging call, then the core message/level/logger.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Correlation context first, so explicit call-site extras can override.
        payload.update(_log_context.get())
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger (idempotent).

    Safe to call from the gateway startup, the CLI, and tests; replaces any
    existing handlers so we never double-emit lines.
    """
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())


def bind_context(**fields: Any) -> contextvars.Token[dict[str, Any]]:
    """Merge ``fields`` into the current correlation context.

    Returns a token so the caller (or :func:`log_context`) can restore the
    previous context, keeping bindings scoped to a task/step.
    """
    merged = {**_log_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    return _log_context.set(merged)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Scope correlation fields to a ``with`` block."""
    token = bind_context(**fields)
    try:
        yield
    finally:
        _log_context.reset(token)


def get_logger(name: str) -> logging.Logger:
    """Module-scoped logger. Use ``get_logger(__name__)`` at import time."""
    return logging.getLogger(name)

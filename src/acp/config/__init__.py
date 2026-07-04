"""Centralized, env-driven configuration (pydantic-settings).

One typed :class:`Settings` object is the single source of truth for every
knob; nothing in the codebase reads ``os.environ`` directly. This makes the
whole system reproducible from ``.env`` alone and keeps the "runs first go,
keyless" property honest — the defaults boot the full stack in stub mode with
no secrets present.
"""

from acp.config.settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]

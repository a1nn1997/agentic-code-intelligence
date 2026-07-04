"""A tiny in-process metrics registry (Phase-0 stub).

Deliberately dependency-free: a dict of counters/gauges rendered in Prometheus
text format. It exists so ``/metrics`` is real from day one (operability
requirement #9) and so later phases have a single place to increment counters
(tasks by terminal state, injections defended, sandbox pass/fail). The Phase-9
dashboard reads this endpoint — never internal state — proving the API-only
boundary.
"""

from __future__ import annotations

import threading


class Metrics:
    """Thread-safe counters + gauges. Not a full Prometheus client — just enough
    to emit valid exposition text and be swapped for one later."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}

    def inc(self, name: str, amount: float = 1.0) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + amount

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def render(self) -> str:
        """Render Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            for name, value in sorted(self._counters.items()):
                lines.append(f"# TYPE {name} counter")
                lines.append(f"{name} {value}")
            for name, value in sorted(self._gauges.items()):
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name} {value}")
        # Phase-0 baseline so the endpoint is never empty.
        if not lines:
            lines = ["# TYPE acp_build_info gauge", "acp_build_info 1"]
        return "\n".join(lines) + "\n"


# Process-wide singleton; modules import and increment this.
METRICS = Metrics()

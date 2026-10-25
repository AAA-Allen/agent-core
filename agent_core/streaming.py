"""In-process token streaming bus between model adapters and SSE consumers.

Nodes publish model deltas keyed by ``run_id``; the HTTP layer replays the
run's history on connect (late subscribers lose nothing) and follows live.
History is capped per run; the oldest deltas are dropped when the cap is hit
and ``truncated`` reports it, so a runaway stream cannot grow memory without
bound. Everything here is process-local by design: cross-process streaming
is out of scope.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Callable

MAX_HISTORY_PER_RUN = 10_000


class TokenBus:
    """Fan-out buffer of model token deltas per run."""

    def __init__(self, *, max_history: int = MAX_HISTORY_PER_RUN) -> None:
        if not isinstance(max_history, int) or max_history <= 0:
            raise ValueError("max_history must be a positive integer")
        self._max_history = max_history
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def open(self, run_id: str) -> None:
        with self._lock:
            self._runs.setdefault(run_id, {
                "events": deque(maxlen=self._max_history),
                "truncated": False,
                "closed": False,
            })

    def publish(self, run_id: str, node_id: str, text: str) -> None:
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None or entry["closed"]:
                return
            if len(entry["events"]) == entry["events"].maxlen:
                entry["truncated"] = True
            entry["events"].append((node_id, text))

    def close(self, run_id: str) -> None:
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is not None:
                entry["closed"] = True

    def forget(self, run_id: str) -> None:
        with self._lock:
            self._runs.pop(run_id, None)

    def history(self, run_id: str, *, after: int = 0) -> tuple[list[tuple[str, str]], bool, bool]:
        """Return events after index ``after``, plus (truncated, closed)."""
        with self._lock:
            entry = self._runs.get(run_id)
            if entry is None:
                return [], False, False
            events = list(entry["events"])
            return events[after:], entry["truncated"], entry["closed"]


_BUS: TokenBus | None = None


def default_bus() -> TokenBus:
    """Process-wide bus shared by nodes and the HTTP layer."""
    global _BUS
    if _BUS is None:
        _BUS = TokenBus()
    return _BUS


__all__ = ["TokenBus", "default_bus", "MAX_HISTORY_PER_RUN"]

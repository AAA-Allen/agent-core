"""Checkpoint contracts for the v0.2 durable runtime foundation.

This module stores resumable state snapshots only. It does not resume or
re-execute a graph by itself, and it makes no exactly-once guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .runtime import _json_snapshot, _validate_run_id


@dataclass(frozen=True)
class Checkpoint:
    """A validated snapshot at a graph execution boundary."""

    run_id: str
    graph_id: str
    next_node: str | None
    state: Mapping[str, Any]
    state_version: int
    step_count: int

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        if not isinstance(self.graph_id, str) or not self.graph_id.strip():
            raise ValueError("graph_id must be a nonempty string")
        if self.next_node is not None and (
            not isinstance(self.next_node, str) or not self.next_node.strip()
        ):
            raise ValueError("next_node must be a nonempty string when provided")
        if not isinstance(self.state_version, int) or isinstance(self.state_version, bool) or self.state_version < 0:
            raise ValueError("state_version must be a nonnegative integer")
        if not isinstance(self.step_count, int) or isinstance(self.step_count, bool) or self.step_count < 0:
            raise ValueError("step_count must be a nonnegative integer")
        object.__setattr__(self, "state", _json_snapshot(self.state))


class CheckpointStore(Protocol):
    def save(self, checkpoint: Checkpoint) -> None: ...

    def load(self, run_id: str) -> Checkpoint | None: ...

    def delete(self, run_id: str) -> None: ...


class InMemoryCheckpointStore:
    """Process-local checkpoint store for tests and early integrations."""

    def __init__(self) -> None:
        self._records: dict[str, Checkpoint] = {}

    def save(self, checkpoint: Checkpoint) -> None:
        if not isinstance(checkpoint, Checkpoint):
            raise TypeError("checkpoint must be a Checkpoint")
        existing = self._records.get(checkpoint.run_id)
        if existing is not None and checkpoint.state_version < existing.state_version:
            raise ValueError("checkpoint state_version cannot move backwards")
        self._records[checkpoint.run_id] = Checkpoint(
            checkpoint.run_id, checkpoint.graph_id, checkpoint.next_node,
            checkpoint.state, checkpoint.state_version, checkpoint.step_count,
        )

    def load(self, run_id: str) -> Checkpoint | None:
        _validate_run_id(run_id)
        checkpoint = self._records.get(run_id)
        if checkpoint is None:
            return None
        return Checkpoint(
            checkpoint.run_id, checkpoint.graph_id, checkpoint.next_node,
            checkpoint.state, checkpoint.state_version, checkpoint.step_count,
        )

    def delete(self, run_id: str) -> None:
        _validate_run_id(run_id)
        self._records.pop(run_id, None)

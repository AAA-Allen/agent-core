"""Checkpoint contracts for the v0.2 durable runtime foundation.

This module stores resumable state snapshots only. It does not resume or
re-execute a graph by itself, and it makes no exactly-once guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import uuid4

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
    schema_version: int = 1

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
        if self.schema_version != 1:
            raise ValueError("unsupported checkpoint schema_version")
        object.__setattr__(self, "state", _json_snapshot(self.state))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "graph_id": self.graph_id,
            "next_node": self.next_node,
            "state": _json_snapshot(self.state),
            "state_version": self.state_version,
            "step_count": self.step_count,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Checkpoint":
        if not isinstance(payload, Mapping):
            raise ValueError("checkpoint payload must be an object")
        required = {"schema_version", "run_id", "graph_id", "next_node", "state", "state_version", "step_count"}
        if not required <= payload.keys():
            raise ValueError("checkpoint payload is missing required fields")
        return cls(
            payload["run_id"], payload["graph_id"], payload["next_node"],
            payload["state"], payload["state_version"], payload["step_count"],
            payload["schema_version"],
        )


class CheckpointStore(Protocol):
    def save(self, checkpoint: Checkpoint) -> None: ...

    def load(self, run_id: str) -> Checkpoint | None: ...

    def load_for(self, run_id: str, graph_id: str) -> Checkpoint | None: ...

    def delete(self, run_id: str) -> None: ...


class InMemoryCheckpointStore:
    """Process-local checkpoint store for tests and early integrations."""

    def __init__(self) -> None:
        self._records: dict[str, Checkpoint] = {}

    def save(self, checkpoint: Checkpoint) -> None:
        if not isinstance(checkpoint, Checkpoint):
            raise TypeError("checkpoint must be a Checkpoint")
        existing = self._records.get(checkpoint.run_id)
        if existing is not None:
            if checkpoint.graph_id != existing.graph_id:
                raise ValueError("checkpoint graph_id cannot change")
            if checkpoint.state_version < existing.state_version:
                raise ValueError("checkpoint state_version cannot move backwards")
            if checkpoint.step_count < existing.step_count:
                raise ValueError("checkpoint step_count cannot move backwards")
            if (
                checkpoint.state_version == existing.state_version
                and checkpoint.step_count == existing.step_count
                and checkpoint != existing
            ):
                raise ValueError("checkpoint cannot change without progress")
        self._records[checkpoint.run_id] = Checkpoint(
            checkpoint.run_id, checkpoint.graph_id, checkpoint.next_node,
            checkpoint.state, checkpoint.state_version, checkpoint.step_count,
            checkpoint.schema_version,
        )

    def load(self, run_id: str) -> Checkpoint | None:
        _validate_run_id(run_id)
        checkpoint = self._records.get(run_id)
        if checkpoint is None:
            return None
        return Checkpoint(
            checkpoint.run_id, checkpoint.graph_id, checkpoint.next_node,
            checkpoint.state, checkpoint.state_version, checkpoint.step_count,
            checkpoint.schema_version,
        )

    def load_for(self, run_id: str, graph_id: str) -> Checkpoint | None:
        if not isinstance(graph_id, str) or not graph_id.strip():
            raise ValueError("graph_id must be a nonempty string")
        checkpoint = self.load(run_id)
        if checkpoint is None:
            return None
        if checkpoint.graph_id != graph_id:
            raise ValueError("checkpoint graph_id does not match requested graph")
        return checkpoint

    def delete(self, run_id: str) -> None:
        _validate_run_id(run_id)
        self._records.pop(run_id, None)


class FileCheckpointStore:
    """Atomic JSON checkpoint store for a single process or simple deployment."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        return self._directory

    def save(self, checkpoint: Checkpoint) -> None:
        if not isinstance(checkpoint, Checkpoint):
            raise TypeError("checkpoint must be a Checkpoint")
        existing = self.load(checkpoint.run_id)
        if existing is not None:
            if checkpoint.graph_id != existing.graph_id:
                raise ValueError("checkpoint graph_id cannot change")
            if checkpoint.state_version < existing.state_version:
                raise ValueError("checkpoint state_version cannot move backwards")
            if checkpoint.step_count < existing.step_count:
                raise ValueError("checkpoint step_count cannot move backwards")
            if (
                checkpoint.state_version == existing.state_version
                and checkpoint.step_count == existing.step_count
                and checkpoint != existing
            ):
                raise ValueError("checkpoint cannot change without progress")
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._directory / f"{checkpoint.run_id}.json"
        temporary = self._directory / f".{checkpoint.run_id}.{uuid4().hex}.json.tmp"
        try:
            temporary.write_text(
                json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2, allow_nan=False),
                encoding="utf-8",
            )
            temporary.replace(target)
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    def load(self, run_id: str) -> Checkpoint | None:
        _validate_run_id(run_id)
        target = self._directory / f"{run_id}.json"
        if not target.is_file():
            return None
        try:
            checkpoint = Checkpoint.from_dict(json.loads(target.read_text(encoding="utf-8")))
            if checkpoint.run_id != run_id:
                raise ValueError("checkpoint run_id does not match filename")
            return checkpoint
        except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
            raise ValueError("invalid checkpoint record") from exc

    def load_for(self, run_id: str, graph_id: str) -> Checkpoint | None:
        if not isinstance(graph_id, str) or not graph_id.strip():
            raise ValueError("graph_id must be a nonempty string")
        checkpoint = self.load(run_id)
        if checkpoint is not None and checkpoint.graph_id != graph_id:
            raise ValueError("checkpoint graph_id does not match requested graph")
        return checkpoint

    def delete(self, run_id: str) -> None:
        _validate_run_id(run_id)
        target = self._directory / f"{run_id}.json"
        try:
            target.unlink()
        except FileNotFoundError:
            pass

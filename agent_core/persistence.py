"""Minimal run persistence: save and load finished workflow runs as JSON files.

Scope is deliberately small: a finished run (COMPLETED, BLOCKED or FAILED) is
written to one JSON file per run_id. No resume, cancel, retry, or event
sourcing — those belong to later stages. Writes are atomic (tmp file plus
rename) so an interrupted save never leaves a half-written record behind.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .runtime import RunResult, RunStatus, TraceEvent, _validate_run_id


class RunStore:
    """Persist finished workflow runs as one JSON file per run_id."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        return self._directory

    def save(self, result: RunResult) -> Path:
        """Write one run record atomically; an existing run_id is overwritten."""
        self._directory.mkdir(parents=True, exist_ok=True)
        _validate_run_id(result.run_id)
        target = self._directory / f"{result.run_id}.json"
        tmp = self._directory / f".{result.run_id}.{uuid4().hex}.json.tmp"
        payload = json.dumps(
            _result_to_dict(result), ensure_ascii=False, indent=2, allow_nan=False
        )
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(target)
        return target

    def load(self, run_id: str) -> RunResult | None:
        """Return a stored run, or None when the run_id was never saved."""
        _validate_run_id(run_id)
        target = self._directory / f"{run_id}.json"
        if not target.is_file():
            return None
        return _dict_to_result(json.loads(target.read_text(encoding="utf-8")))

    def list_runs(self) -> list[str]:
        """Return saved run_ids in filename order."""
        if not self._directory.is_dir():
            return []
        return sorted(path.stem for path in self._directory.glob("*.json"))


def _result_to_dict(result: RunResult) -> dict:
    return {
        "run_id": result.run_id,
        "status": result.status.value,
        "outputs": dict(result.outputs),
        "trace": [asdict(event) for event in result.trace],
        "stopped_at": result.stopped_at,
        "reason_code": result.reason_code,
        "state": dict(result.state if result.state is not None else result.outputs),
        "simulated": result.simulated,
    }


def _dict_to_result(payload: dict) -> RunResult:
    trace = tuple(
        TraceEvent(
            run_id=event["run_id"],
            step=event["step"],
            status=event["status"],
            started_at=event["started_at"],
            duration_ms=event["duration_ms"],
            reason_code=event.get("reason_code"),
            error_type=event.get("error_type"),
            event_id=event.get("event_id"),
            event_type=event.get("event_type"),
            sequence=event.get("sequence", 0),
            node_id=event.get("node_id"),
            execution_id=event.get("execution_id"),
            state_version=event.get("state_version", 0),
            metadata=event.get("metadata", {}),
        )
        for event in payload["trace"]
    )
    return RunResult(
        run_id=payload["run_id"],
        status=RunStatus(payload["status"]),
        outputs=payload["outputs"],
        trace=trace,
        stopped_at=payload.get("stopped_at"),
        reason_code=payload.get("reason_code"),
        state=payload.get("state", payload["outputs"]),
        simulated=payload.get("simulated", False),
    )

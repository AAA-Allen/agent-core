"""Minimal run persistence: save workflow records and lifecycle events as JSON files.

Scope is deliberately small: a finished run is written to one run record per
run_id, while lifecycle events are also available as a separate atomic log so
running executions can be queried. It is not an event-sourcing store and does
not provide automatic resume, retry, or cancellation.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .runtime import RunResult, RunStatus, TraceEvent, _validate_run_id
from .lifecycle import LifecycleEvent, LifecycleStatus, RunLifecycle


class RunStore:
    """Persist terminal workflow results and lifecycle events per run_id."""

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
        try:
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(target)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return target

    def load(self, run_id: str) -> RunResult | None:
        """Return a stored run, or None when the run_id was never saved."""
        _validate_run_id(run_id)
        target = self._directory / f"{run_id}.json"
        if not target.is_file():
            return None
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            result = _dict_to_result(payload)
            if result.run_id != run_id:
                raise ValueError("run_id does not match record filename")
            return result
        except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
            raise ValueError("invalid run record") from exc

    def list_runs(self) -> list[str]:
        """Return saved run_ids in filename order."""
        if not self._directory.is_dir():
            return []
        return sorted(
            path.stem for path in self._directory.glob("*.json")
            if not path.name.endswith(".events.json")
        )

    def save_lifecycle_events(self, run_id: str, events: tuple[LifecycleEvent, ...]) -> Path:
        """Atomically persist the lifecycle event sequence for a run."""
        _validate_run_id(run_id)
        if not events:
            raise ValueError("lifecycle event sequence cannot be empty")
        for expected_sequence, event in enumerate(events, start=1):
            if event.run_id != run_id or event.sequence != expected_sequence:
                raise ValueError("invalid lifecycle event sequence")
        RunLifecycle.from_events(events)
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._directory / f"{run_id}.events.json"
        tmp = self._directory / f".{run_id}.{uuid4().hex}.events.json.tmp"
        try:
            tmp.write_text(
                json.dumps({
                    "run_id": run_id,
                    "events": [_lifecycle_event_to_dict(event) for event in events],
                }, ensure_ascii=False, indent=2, allow_nan=False),
                encoding="utf-8",
            )
            tmp.replace(target)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return target

    def load_lifecycle_events(self, run_id: str) -> tuple[LifecycleEvent, ...]:
        """Load lifecycle events, returning an empty sequence when absent."""
        _validate_run_id(run_id)
        target = self._directory / f"{run_id}.events.json"
        if not target.is_file():
            return ()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("run_id") != run_id:
                raise ValueError("lifecycle run_id does not match filename")
            events = tuple(_dict_to_lifecycle_event(event) for event in payload["events"])
            for expected_sequence, event in enumerate(events, start=1):
                if event.run_id != run_id or event.sequence != expected_sequence:
                    raise ValueError("invalid lifecycle event sequence")
            return RunLifecycle.from_events(events).events
        except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
            raise ValueError("invalid lifecycle event record") from exc


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
        "lifecycle_events": [_lifecycle_event_to_dict(event) for event in result.lifecycle_events],
    }


def _dict_to_result(payload: dict) -> RunResult:
    if not isinstance(payload, dict):
        raise TypeError("run record must be an object")
    required = {"run_id", "status", "outputs", "trace"}
    if not required <= payload.keys():
        raise KeyError("missing run record field")
    if not isinstance(payload["trace"], list):
        raise TypeError("trace must be a list")
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
    lifecycle_events = tuple(
        _dict_to_lifecycle_event(event) for event in payload.get("lifecycle_events", [])
    )
    if lifecycle_events:
        lifecycle_events = RunLifecycle.from_events(lifecycle_events).events
        if lifecycle_events[-1].to_status.value != payload["status"]:
            raise ValueError("run status does not match lifecycle event history")
    for expected_sequence, event in enumerate(lifecycle_events, start=1):
        if event.run_id != payload["run_id"]:
            raise ValueError("lifecycle event run_id does not match run record")
        if event.sequence != expected_sequence:
            raise ValueError("lifecycle event sequence is not contiguous")
    return RunResult(
        run_id=payload["run_id"],
        status=RunStatus(payload["status"]),
        outputs=payload["outputs"],
        trace=trace,
        stopped_at=payload.get("stopped_at"),
        reason_code=payload.get("reason_code"),
        state=payload.get("state", payload["outputs"]),
        simulated=payload.get("simulated", False),
        lifecycle_events=lifecycle_events,
    )


def _lifecycle_event_to_dict(event: LifecycleEvent) -> dict:
    return {
        "run_id": event.run_id,
        "sequence": event.sequence,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "from_status": None if event.from_status is None else event.from_status.value,
        "to_status": event.to_status.value,
        "occurred_at": event.occurred_at,
        "reason_code": event.reason_code,
    }


def _dict_to_lifecycle_event(payload: dict) -> LifecycleEvent:
    if not isinstance(payload, dict):
        raise TypeError("lifecycle event must be an object")
    return LifecycleEvent(
        run_id=payload["run_id"],
        sequence=payload["sequence"],
        event_id=payload["event_id"],
        event_type=payload["event_type"],
        from_status=(None if payload.get("from_status") is None
                     else LifecycleStatus(payload["from_status"])),
        to_status=LifecycleStatus(payload["to_status"]),
        occurred_at=payload["occurred_at"],
        reason_code=payload.get("reason_code"),
    )

"""Run lifecycle state machine used by the synchronous runtime.

The state machine owns only lifecycle facts and transition validation. It does
not execute nodes, cancel Python code, or persist records by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from .runtime import _validate_run_id


class LifecycleStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"

    @property
    def terminal(self) -> bool:
        return self in {
            LifecycleStatus.COMPLETED,
            LifecycleStatus.BLOCKED,
            LifecycleStatus.FAILED,
            LifecycleStatus.CANCELED,
        }


_ALLOWED_TRANSITIONS: dict[LifecycleStatus, frozenset[LifecycleStatus]] = {
    LifecycleStatus.PENDING: frozenset({LifecycleStatus.RUNNING, LifecycleStatus.CANCELED}),
    LifecycleStatus.RUNNING: frozenset({
        LifecycleStatus.COMPLETED,
        LifecycleStatus.BLOCKED,
        LifecycleStatus.FAILED,
        LifecycleStatus.CANCELED,
    }),
    LifecycleStatus.COMPLETED: frozenset(),
    LifecycleStatus.BLOCKED: frozenset(),
    LifecycleStatus.FAILED: frozenset(),
    LifecycleStatus.CANCELED: frozenset(),
}


@dataclass(frozen=True)
class LifecycleEvent:
    run_id: str
    sequence: int
    event_id: str
    event_type: str
    from_status: LifecycleStatus | None
    to_status: LifecycleStatus
    occurred_at: str
    reason_code: str | None = None


class RunLifecycle:
    """Validate and record one run's lifecycle transitions."""

    def __init__(self, run_id: str) -> None:
        _validate_run_id(run_id)
        self.run_id = run_id
        self._status = LifecycleStatus.PENDING
        self._events: list[LifecycleEvent] = [
            LifecycleEvent(
                run_id=run_id,
                sequence=1,
                event_id=uuid4().hex,
                event_type="run.created",
                from_status=None,
                to_status=LifecycleStatus.PENDING,
                occurred_at=datetime.now(timezone.utc).isoformat(),
            )
        ]

    @property
    def status(self) -> LifecycleStatus:
        return self._status

    @property
    def terminal(self) -> bool:
        return self._status.terminal

    @property
    def events(self) -> tuple[LifecycleEvent, ...]:
        return tuple(self._events)

    def transition(self, target: LifecycleStatus, *, reason_code: str | None = None) -> LifecycleEvent:
        if not isinstance(target, LifecycleStatus):
            raise TypeError("target must be a LifecycleStatus")
        if reason_code is not None and (
            not isinstance(reason_code, str) or not reason_code.strip()
        ):
            raise ValueError("reason_code must be a nonempty string when provided")
        allowed = _ALLOWED_TRANSITIONS[self._status]
        if target not in allowed:
            raise ValueError(f"invalid lifecycle transition: {self._status.value} -> {target.value}")
        previous = self._status
        self._status = target
        event = LifecycleEvent(
            run_id=self.run_id,
            sequence=len(self._events) + 1,
            event_id=uuid4().hex,
            event_type=f"run.{target.value.lower()}",
            from_status=previous,
            to_status=target,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            reason_code=reason_code,
        )
        self._events.append(event)
        return event

    def resume(self) -> LifecycleEvent:
        """Start an explicit recovery attempt from a resumable terminal run."""
        if self._status not in {LifecycleStatus.FAILED, LifecycleStatus.BLOCKED}:
            raise ValueError(
                f"run cannot resume from lifecycle status: {self._status.value}"
            )
        previous = self._status
        self._status = LifecycleStatus.RUNNING
        event = LifecycleEvent(
            run_id=self.run_id,
            sequence=len(self._events) + 1,
            event_id=uuid4().hex,
            event_type="run.resumed",
            from_status=previous,
            to_status=LifecycleStatus.RUNNING,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            reason_code="RESUME",
        )
        self._events.append(event)
        return event

    @classmethod
    def from_events(cls, events: tuple[LifecycleEvent, ...]) -> "RunLifecycle":
        """Rehydrate a lifecycle from a validated persisted event sequence."""
        if not events:
            raise ValueError("lifecycle event sequence cannot be empty")
        run_id = events[0].run_id
        _validate_run_id(run_id)
        for expected_sequence, event in enumerate(events, start=1):
            if event.run_id != run_id or event.sequence != expected_sequence:
                raise ValueError("invalid lifecycle event sequence")
        first = events[0]
        if first.event_type != "run.created" or first.from_status is not None \
                or first.to_status is not LifecycleStatus.PENDING:
            raise ValueError("lifecycle event sequence must start with run.created")
        status = LifecycleStatus.PENDING
        for event in events[1:]:
            if event.from_status is not status:
                raise ValueError("lifecycle event transition source is invalid")
            if event.event_type == "run.resumed":
                if status not in {LifecycleStatus.FAILED, LifecycleStatus.BLOCKED} \
                        or event.to_status is not LifecycleStatus.RUNNING:
                    raise ValueError("lifecycle resume transition is invalid")
            elif event.to_status not in _ALLOWED_TRANSITIONS[status]:
                raise ValueError("lifecycle event transition is invalid")
            status = event.to_status
        instance = cls.__new__(cls)
        instance.run_id = run_id
        instance._status = status
        instance._events = list(events)
        return instance


__all__ = ["LifecycleEvent", "LifecycleStatus", "RunLifecycle"]

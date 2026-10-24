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
        event_ids: set[str] = set()
        for expected_sequence, event in enumerate(events, start=1):
            if event.run_id != run_id or event.sequence != expected_sequence:
                raise ValueError("invalid lifecycle event sequence")
            if not isinstance(event.event_id, str) or not event.event_id.strip() \
                    or event.event_id in event_ids:
                raise ValueError("lifecycle event ids must be unique")
            if not isinstance(event.occurred_at, str) or not event.occurred_at.strip():
                raise ValueError("lifecycle event occurred_at must be nonempty")
            try:
                datetime.fromisoformat(event.occurred_at)
            except (TypeError, ValueError) as exc:
                raise ValueError("lifecycle event occurred_at must be ISO-8601") from exc
            event_ids.add(event.event_id)
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
            elif event.event_type == "run.recovered":
                if status is not LifecycleStatus.RUNNING \
                        or event.to_status is not LifecycleStatus.RUNNING:
                    raise ValueError("lifecycle recovery transition is invalid")
            else:
                expected_event_type = f"run.{event.to_status.value.lower()}"
                if event.event_type != expected_event_type \
                        or event.to_status not in _ALLOWED_TRANSITIONS[status]:
                    raise ValueError("lifecycle event transition is invalid")
            status = event.to_status
        instance = cls.__new__(cls)
        instance.run_id = run_id
        instance._status = status
        instance._events = list(events)
        return instance

    def recover(self) -> LifecycleEvent:
        """Record explicit recovery of a persisted RUNNING execution."""
        if self._status is not LifecycleStatus.RUNNING:
            raise ValueError(
                f"run cannot recover from lifecycle status: {self._status.value}"
            )
        event = LifecycleEvent(
            run_id=self.run_id,
            sequence=len(self._events) + 1,
            event_id=uuid4().hex,
            event_type="run.recovered",
            from_status=LifecycleStatus.RUNNING,
            to_status=LifecycleStatus.RUNNING,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            reason_code="RECOVERY",
        )
        self._events.append(event)
        return event


__all__ = ["LifecycleEvent", "LifecycleStatus", "RunLifecycle"]

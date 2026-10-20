"""Small domain-neutral Agent workflow runtime.

The runtime owns execution order and trace provenance. A step owns its domain
logic and returns an explicit outcome. No LLM, model, or inspection vocabulary
is required by the core.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable, Mapping
from uuid import uuid4

if TYPE_CHECKING:
    from .persistence import RunStore


class RunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class StepStatus(str, Enum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class StepOutcome:
    status: StepStatus
    output: Mapping[str, Any] = field(default_factory=dict)
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, StepStatus):
            raise TypeError("status must be a StepStatus")
        if not isinstance(self.output, Mapping):
            raise TypeError("output must be a mapping")
        if self.reason_code is not None and (
            not isinstance(self.reason_code, str) or not self.reason_code.strip()
        ):
            raise ValueError("reason_code must be a nonempty string when provided")
        if self.status is not StepStatus.COMPLETED and not self.reason_code:
            raise ValueError("non-completed outcomes require a reason_code")


@dataclass(frozen=True)
class Step:
    name: str
    execute: Callable[[RunContext], StepOutcome]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("step name must be nonempty")
        if not callable(self.execute):
            raise TypeError("step execute must be callable")


@dataclass
class RunContext:
    run_id: str
    _input: dict[str, Any] = field(repr=False)
    _outputs: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    @property
    def input(self) -> Mapping[str, Any]:
        return _json_snapshot(self._input)

    @property
    def outputs(self) -> Mapping[str, Mapping[str, Any]]:
        return _json_snapshot(self._outputs)

    def output_of(self, step_name: str) -> Mapping[str, Any]:
        return _json_snapshot(self._outputs[step_name])


@dataclass(frozen=True)
class TraceEvent:
    run_id: str
    step: str
    status: str
    started_at: str
    duration_ms: float
    reason_code: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: RunStatus
    outputs: Mapping[str, Mapping[str, Any]]
    trace: tuple[TraceEvent, ...]
    stopped_at: str | None = None
    reason_code: str | None = None


class WorkflowRunner:
    """Execute registered steps once, in order, with fail-closed stop behavior.

    An optional RunStore persists every finished run (COMPLETED, BLOCKED or
    FAILED) as one JSON record per run_id. Persistence failures surface as
    exceptions from run(); the run result itself is already produced.
    """

    def __init__(
        self,
        steps: list[Step] | tuple[Step, ...],
        *,
        store: "RunStore | None" = None,
    ) -> None:
        if not steps:
            raise ValueError("workflow needs at least one step")
        if any(not isinstance(step, Step) for step in steps):
            raise TypeError("workflow steps must be Step instances")
        names = [step.name for step in steps]
        if len(set(names)) != len(names):
            raise ValueError("step names must be unique")
        self.steps = tuple(steps)
        self._store = store

    def run(self, input_data: Mapping[str, Any], *, run_id: str | None = None) -> RunResult:
        if not isinstance(input_data, Mapping):
            raise TypeError("input_data must be a mapping")
        identity = uuid4().hex if run_id is None else run_id
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 128:
            raise ValueError("run_id must contain 1-128 non-whitespace characters")
        context = RunContext(run_id=identity, _input=_json_snapshot(input_data))
        result = self._execute(context)
        if self._store is not None:
            self._store.save(result)
        return result

    def _execute(self, context: RunContext) -> RunResult:
        events: list[TraceEvent] = []
        for step in self.steps:
            started_at = datetime.now(timezone.utc).isoformat()
            started = monotonic()
            try:
                outcome = step.execute(context)
                if not isinstance(outcome, StepOutcome):
                    raise TypeError("step must return StepOutcome")
                context._outputs[step.name] = _json_snapshot(outcome.output)
                events.append(TraceEvent(
                    context.run_id, step.name, outcome.status.value, started_at,
                    round((monotonic() - started) * 1000, 3), outcome.reason_code,
                ))
                if outcome.status is StepStatus.BLOCKED:
                    return RunResult(context.run_id, RunStatus.BLOCKED, _json_snapshot(context.outputs),
                                     tuple(events), step.name, outcome.reason_code)
                if outcome.status is StepStatus.FAILED:
                    return RunResult(context.run_id, RunStatus.FAILED, _json_snapshot(context.outputs),
                                     tuple(events), step.name, outcome.reason_code)
            except Exception as exc:
                events.append(TraceEvent(
                    context.run_id, step.name, RunStatus.FAILED.value, started_at,
                    round((monotonic() - started) * 1000, 3), "STEP_EXCEPTION",
                    type(exc).__name__,
                ))
                return RunResult(context.run_id, RunStatus.FAILED, _json_snapshot(context.outputs),
                                 tuple(events), step.name, "STEP_EXCEPTION")
        return RunResult(context.run_id, RunStatus.COMPLETED, _json_snapshot(context.outputs), tuple(events))


def _json_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    """Detach step data and reject non-finite or non-JSON workflow payloads."""
    return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))

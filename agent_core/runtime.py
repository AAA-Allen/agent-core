"""Small domain-neutral Agent workflow runtime.

The runtime owns execution order and trace provenance. A step owns its domain
logic and returns an explicit outcome. No LLM, model, or inspection vocabulary
is required by the core.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable, Mapping
from uuid import uuid4

if TYPE_CHECKING:
    from .persistence import RunStore
    from .lifecycle import LifecycleEvent, LifecycleStatus, RunLifecycle


class RunStatus(str, Enum):
    CANCELED = "CANCELED"
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
    event_id: str | None = None
    event_type: str | None = None
    sequence: int = 0
    node_id: str | None = None
    execution_id: str | None = None
    state_version: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: RunStatus
    outputs: Mapping[str, Any]
    trace: tuple[TraceEvent, ...]
    stopped_at: str | None = None
    reason_code: str | None = None
    state: Mapping[str, Any] | None = None
    simulated: bool = False
    lifecycle_events: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if self.state is None:
            object.__setattr__(self, "state", self.outputs)


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
        _validate_run_id(identity)
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
    _validate_json_keys(value)
    return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))


def _validate_json_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        for item in value.values():
            _validate_json_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_keys(item)


_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError(
            "run_id must be 1-128 characters using letters, digits, '-' or '_'"
        )


class Runtime:
    """Synchronous v0.1 graph executor.

    This is intentionally separate from WorkflowRunner so the existing linear
    API remains compatible while Graph Runtime behavior is introduced. It
    supports optional boundary checkpoints, explicit resume, and cooperative
    cancellation/timeout; it does not provide retries or parallelism.
    """

    def __init__(self, *, max_steps: int = 100, checkpoint_store: Any = None,
                 graph_id: str = "graph", timeout_seconds: float | None = None,
                 store: "RunStore | None" = None) -> None:
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps <= 0:
            raise ValueError("max_steps must be a positive integer")
        self.max_steps = max_steps
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive when provided")
        self.timeout_seconds = timeout_seconds
        if checkpoint_store is not None and not all(
            callable(getattr(checkpoint_store, name, None))
            for name in ("save", "load", "delete")
        ):
            raise TypeError("checkpoint_store must implement save, load and delete")
        if not isinstance(graph_id, str) or not graph_id.strip():
            raise ValueError("graph_id must be a nonempty string")
        self._checkpoint_store = checkpoint_store
        self._graph_id = graph_id
        if store is not None and not all(
            callable(getattr(store, name, None)) for name in ("save", "load")
        ):
            raise TypeError("store must implement save and load")
        self._store = store
        self._lifecycles: dict[str, "RunLifecycle"] = {}
        self._cancel_requests: set[str] = set()
        self._cancel_lock = threading.Lock()

    def run(self, graph: Any, input: Mapping[str, Any], *, run_id: str | None = None) -> RunResult:
        """Execute a compiled graph synchronously until a terminal result."""
        identity = uuid4().hex if run_id is None else run_id
        lifecycle = self._begin_lifecycle(identity)
        try:
            result = self._execute(graph, input, run_id=identity)
        except Exception:
            self._finish_lifecycle(lifecycle, RunStatus.FAILED, reason_code="RUNTIME_EXCEPTION")
            self._persist_lifecycle(lifecycle)
            raise
        self._finish_lifecycle(lifecycle, result.status, reason_code=result.reason_code)
        self._persist_lifecycle(lifecycle)
        result = replace(result, lifecycle_events=lifecycle.events)
        self._persist_result(result)
        with self._cancel_lock:
            self._cancel_requests.discard(identity)
        return result

    def resume(self, graph: Any, checkpoint: Any) -> RunResult:
        """Continue from a compatible checkpoint at its next node."""
        from .checkpoint import Checkpoint
        from .graph import CompiledGraph

        if not isinstance(checkpoint, Checkpoint):
            raise TypeError("checkpoint must be a Checkpoint")
        if not isinstance(graph, CompiledGraph):
            raise TypeError("graph must be a compiled Graph")
        if checkpoint.graph_id != self._graph_id:
            raise ValueError("checkpoint graph_id does not match runtime graph_id")
        if checkpoint.next_node is None:
            raise ValueError("checkpoint has no next_node to resume")
        if checkpoint.next_node not in graph.nodes:
            raise ValueError("checkpoint next_node does not exist in graph")
        lifecycle = self._lifecycles.get(checkpoint.run_id)
        if lifecycle is None:
            lifecycle = self._begin_lifecycle(checkpoint.run_id)
        else:
            lifecycle.resume()
            self._persist_lifecycle(lifecycle)
        try:
            result = self._execute(
                graph, checkpoint.state, run_id=checkpoint.run_id,
                initial_state=checkpoint.state, current=checkpoint.next_node,
                state_version=checkpoint.state_version,
                step_offset=checkpoint.step_count,
            )
        except Exception:
            self._finish_lifecycle(lifecycle, RunStatus.FAILED, reason_code="RUNTIME_EXCEPTION")
            self._persist_lifecycle(lifecycle)
            raise
        self._finish_lifecycle(lifecycle, result.status, reason_code=result.reason_code)
        self._persist_lifecycle(lifecycle)
        result = replace(result, lifecycle_events=lifecycle.events)
        self._persist_result(result)
        with self._cancel_lock:
            self._cancel_requests.discard(checkpoint.run_id)
        return result

    def get_status(self, run_id: str):
        """Return the known lifecycle status, or ``None`` for an unknown run."""
        from .lifecycle import LifecycleStatus

        _validate_run_id(run_id)
        lifecycle = self._lifecycles.get(run_id)
        if lifecycle is not None:
            return lifecycle.status
        if self._store is not None:
            load_events = getattr(self._store, "load_lifecycle_events", None)
            if callable(load_events):
                events = load_events(run_id)
                if events:
                    from .lifecycle import LifecycleStatus
                    return events[-1].to_status
            stored = self._store.load(run_id)
            if stored is not None:
                from .lifecycle import LifecycleStatus
                return LifecycleStatus(stored.status.value)
        return None

    def get_lifecycle_events(self, run_id: str) -> tuple["LifecycleEvent", ...]:
        """Return lifecycle events for a known run in sequence order."""
        _validate_run_id(run_id)
        lifecycle = self._lifecycles.get(run_id)
        if lifecycle is not None:
            return lifecycle.events
        if self._store is not None:
            load_events = getattr(self._store, "load_lifecycle_events", None)
            if callable(load_events):
                events = load_events(run_id)
                if events:
                    return events
            stored = self._store.load(run_id)
            if stored is not None:
                return stored.lifecycle_events
        return ()

    def _persist_result(self, result: RunResult) -> None:
        if self._store is not None:
            self._store.save(result)

    def _persist_lifecycle(self, lifecycle: Any) -> None:
        if self._store is None:
            return
        save_events = getattr(self._store, "save_lifecycle_events", None)
        if callable(save_events):
            save_events(lifecycle.run_id, lifecycle.events)

    def cancel(self, run_id: str) -> bool:
        """Request cooperative cancellation; return whether it was accepted."""
        _validate_run_id(run_id)
        lifecycle = self._lifecycles.get(run_id)
        if lifecycle is None or lifecycle.terminal:
            return False
        with self._cancel_lock:
            self._cancel_requests.add(run_id)
        return True

    def _is_cancel_requested(self, run_id: str) -> bool:
        with self._cancel_lock:
            return run_id in self._cancel_requests

    def _stop_reason(self, run_id: str, started_monotonic: float) -> str | None:
        if self._is_cancel_requested(run_id):
            return "CANCEL_REQUESTED"
        if self.timeout_seconds is not None and monotonic() - started_monotonic >= self.timeout_seconds:
            return "TIMEOUT"
        return None

    def _begin_lifecycle(self, run_id: str):
        from .lifecycle import RunLifecycle, LifecycleStatus

        _validate_run_id(run_id)
        if run_id in self._lifecycles:
            raise ValueError("run_id already has a lifecycle")
        lifecycle = RunLifecycle(run_id)
        lifecycle.transition(LifecycleStatus.RUNNING)
        self._lifecycles[run_id] = lifecycle
        self._persist_lifecycle(lifecycle)
        return lifecycle

    @staticmethod
    def _finish_lifecycle(lifecycle: Any, status: RunStatus, *, reason_code: str | None) -> None:
        from .lifecycle import LifecycleStatus

        target = {
            RunStatus.CANCELED: LifecycleStatus.CANCELED,
            RunStatus.COMPLETED: LifecycleStatus.COMPLETED,
            RunStatus.BLOCKED: LifecycleStatus.BLOCKED,
            RunStatus.FAILED: LifecycleStatus.FAILED,
        }[status]
        lifecycle.transition(target, reason_code=reason_code)

    def _execute(
        self, graph: Any, input: Mapping[str, Any], *, run_id: str,
        initial_state: Mapping[str, Any] | None = None,
        current: str | None = None, state_version: int = 0,
        step_offset: int = 0,
    ) -> RunResult:
        """Execute from an entry node or a validated checkpoint boundary."""
        from .graph import NodeContext, CompiledGraph
        from .results import NodeResult, NodeStatus
        from .checkpoint import Checkpoint

        if not isinstance(graph, CompiledGraph):
            raise TypeError("graph must be a compiled Graph")
        if not isinstance(input, Mapping):
            raise TypeError("input must be a mapping")
        identity = run_id
        _validate_run_id(identity)

        state: dict[str, Any] = _json_snapshot(input if initial_state is None else initial_state)
        trace: list[TraceEvent] = []
        sequence = 0
        simulated = False
        started_monotonic = monotonic()

        def emit(event_type: str, *, node_id: str | None = None,
                 execution_id: str | None = None, status: str | None = None,
                 reason_code: str | None = None) -> None:
            nonlocal sequence
            sequence += 1
            timestamp = datetime.now(timezone.utc).isoformat()
            trace.append(TraceEvent(
                run_id=identity, step=node_id or "", status=status or event_type,
                started_at=timestamp, duration_ms=0.0, reason_code=reason_code,
                event_id=uuid4().hex, event_type=event_type, sequence=sequence,
                node_id=node_id, execution_id=execution_id,
                state_version=state_version,
            ))

        emit("run.started")
        current = graph.entry if current is None else current
        remaining_steps = self.max_steps - step_offset
        if remaining_steps <= 0:
            emit("run.failed", node_id=current, status=RunStatus.FAILED.value,
                 reason_code="MAX_STEPS_EXCEEDED")
            return RunResult(identity, RunStatus.FAILED, _json_snapshot(state),
                             tuple(trace), current, "MAX_STEPS_EXCEEDED",
                             _json_snapshot(state), simulated)
        for step_number in range(remaining_steps):
            stop_reason = self._stop_reason(identity, started_monotonic)
            if stop_reason is not None:
                emit("run.canceled", node_id=current, status=RunStatus.CANCELED.value,
                     reason_code=stop_reason)
                return RunResult(identity, RunStatus.CANCELED, _json_snapshot(state),
                                 tuple(trace), current, stop_reason,
                                 _json_snapshot(state), simulated)
            node = graph.nodes[current]
            execution_id = uuid4().hex
            emit("node.started", node_id=current, execution_id=execution_id)
            try:
                context = NodeContext(
                    run_id=identity, node_id=current, execution_id=execution_id,
                    state_version=state_version,
                    _input=_json_snapshot(input), _state=_json_snapshot(state),
                    _cancel_requested=lambda: self._is_cancel_requested(identity),
                )
                result = node.execute(context)
                stop_reason = self._stop_reason(identity, started_monotonic)
                if stop_reason is not None:
                    emit("run.canceled", node_id=current, execution_id=execution_id,
                         status=RunStatus.CANCELED.value, reason_code=stop_reason)
                    return RunResult(identity, RunStatus.CANCELED, _json_snapshot(state),
                                     tuple(trace), current, stop_reason,
                                     _json_snapshot(state), simulated)
                if not isinstance(result, NodeResult):
                    emit("node.failed", node_id=current, execution_id=execution_id,
                         status=RunStatus.FAILED.value, reason_code="INVALID_NODE_RESULT")
                    emit("run.failed", node_id=current, status=RunStatus.FAILED.value,
                         reason_code="INVALID_NODE_RESULT")
                    return RunResult(identity, RunStatus.FAILED, _json_snapshot(state),
                                     tuple(trace), current, "INVALID_NODE_RESULT",
                                     _json_snapshot(state), simulated)
                if result.status is NodeStatus.SUCCEEDED:
                    try:
                        updates = _json_snapshot(result.updates)
                    except (TypeError, ValueError, OverflowError):
                        emit("node.failed", node_id=current, execution_id=execution_id,
                             status=RunStatus.FAILED.value, reason_code="INVALID_STATE_UPDATE")
                        emit("run.failed", node_id=current, status=RunStatus.FAILED.value,
                             reason_code="INVALID_STATE_UPDATE")
                        return RunResult(identity, RunStatus.FAILED, _json_snapshot(state),
                                         tuple(trace), current, "INVALID_STATE_UPDATE",
                                         _json_snapshot(state), simulated)
                    candidate = dict(state)
                    candidate.update(updates)
                    state = _json_snapshot(candidate)
                    state_version += 1
                    simulated = simulated or result.simulated
                    emit("state.committed", node_id=current, execution_id=execution_id)
                    emit("node.completed", node_id=current, execution_id=execution_id,
                         status=result.status.value)
                else:
                    emit(
                        "node.blocked" if result.status is NodeStatus.BLOCKED else "node.failed",
                        node_id=current, execution_id=execution_id,
                        status=result.status.value, reason_code=result.reason_code,
                    )
                    final_status = RunStatus.BLOCKED if result.status is NodeStatus.BLOCKED else RunStatus.FAILED
                    emit("run.blocked" if final_status is RunStatus.BLOCKED else "run.failed",
                         node_id=current, status=final_status.value, reason_code=result.reason_code)
                    return RunResult(identity, final_status, _json_snapshot(state),
                                     tuple(trace), current, result.reason_code,
                                     _json_snapshot(state), simulated)
            except Exception:
                emit("node.failed", node_id=current, execution_id=execution_id,
                     status=RunStatus.FAILED.value, reason_code="NODE_EXCEPTION")
                emit("run.failed", node_id=current, status=RunStatus.FAILED.value,
                     reason_code="NODE_EXCEPTION")
                return RunResult(identity, RunStatus.FAILED, _json_snapshot(state),
                                 tuple(trace), current, "NODE_EXCEPTION",
                                 _json_snapshot(state), simulated)

            edges = graph.outgoing(current)
            if not edges:
                checkpoint_error = self._save_checkpoint(
                    Checkpoint(identity, self._graph_id, None, state, state_version, step_offset + step_number + 1)
                )
                if checkpoint_error is not None:
                    emit("run.failed", node_id=current, status=RunStatus.FAILED.value,
                         reason_code=checkpoint_error)
                    return RunResult(identity, RunStatus.FAILED, _json_snapshot(state),
                                     tuple(trace), current, checkpoint_error,
                                     _json_snapshot(state), simulated)
                emit("run.completed", node_id=current, status=RunStatus.COMPLETED.value)
                return RunResult(identity, RunStatus.COMPLETED, _json_snapshot(state),
                                 tuple(trace), None, None, _json_snapshot(state), simulated)
            route = result.route
            matches = [edge for edge in edges if edge.when == route]
            if len(matches) == 0:
                code = "NO_ROUTE"
                emit("run.failed", node_id=current, status=RunStatus.FAILED.value, reason_code=code)
                return RunResult(identity, RunStatus.FAILED, _json_snapshot(state),
                                 tuple(trace), current, code, _json_snapshot(state), simulated)
            if len(matches) > 1:
                code = "AMBIGUOUS_ROUTE"
                emit("run.failed", node_id=current, status=RunStatus.FAILED.value, reason_code=code)
                return RunResult(identity, RunStatus.FAILED, _json_snapshot(state),
                                 tuple(trace), current, code, _json_snapshot(state), simulated)
            next_node = matches[0].target
            checkpoint_error = self._save_checkpoint(
                Checkpoint(identity, self._graph_id, next_node, state, state_version, step_offset + step_number + 1)
            )
            if checkpoint_error is not None:
                emit("run.failed", node_id=current, status=RunStatus.FAILED.value,
                     reason_code=checkpoint_error)
                return RunResult(identity, RunStatus.FAILED, _json_snapshot(state),
                                 tuple(trace), current, checkpoint_error,
                                 _json_snapshot(state), simulated)
            emit("route.selected", node_id=current, status=RunStatus.COMPLETED.value)
            current = next_node

        code = "MAX_STEPS_EXCEEDED"
        emit("run.failed", node_id=current, status=RunStatus.FAILED.value, reason_code=code)
        return RunResult(identity, RunStatus.FAILED, _json_snapshot(state),
                         tuple(trace), current, code, _json_snapshot(state), simulated)

    def _save_checkpoint(self, checkpoint: Any) -> str | None:
        if self._checkpoint_store is None:
            return None
        try:
            self._checkpoint_store.save(checkpoint)
        except Exception:
            return "CHECKPOINT_SAVE_FAILED"
        return None

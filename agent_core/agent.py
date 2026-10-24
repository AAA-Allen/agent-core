"""Composable Agent shell built on top of the graph runtime.

The shell deliberately does not implement an LLM, planner, or transport. Those
are application concerns and are injected through small protocols. This keeps
the runtime deterministic while giving applications one place to assemble an
agent's instructions, tools, model, memory, and graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from .graph import Graph, NodeContext
from .results import NodeResult
from .runtime import RunResult, Runtime, _json_snapshot
from .tools import ToolRegistry


class ModelAdapter(Protocol):
    """Application supplied model boundary.

    Implementations may return any JSON serializable mapping. The shell does
    not call the adapter automatically; nodes decide when model work is part
    of their workflow.
    """

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: ToolRegistry | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


class MemoryStore(Protocol):
    """Minimal memory boundary available to Agent nodes."""

    def read(self, key: str, default: Any = None) -> Any: ...

    def write(self, key: str, value: Any) -> None: ...


class InMemoryMemory:
    """Small process-local memory implementation for development and tests."""

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        if initial is not None and not isinstance(initial, Mapping):
            raise TypeError("initial memory must be a mapping")
        self._values = _json_snapshot(initial or {})

    def read(self, key: str, default: Any = None) -> Any:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("memory key must be nonempty")
        value = self._values.get(key, default)
        if isinstance(value, Mapping):
            return _json_snapshot(value)
        return value

    def write(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("memory key must be nonempty")
        self._values[key] = _json_snapshot({"value": value})["value"]


@dataclass(frozen=True)
class AgentNodeContext:
    """Node context plus the dependencies assembled by an :class:`Agent`."""

    run_id: str
    node_id: str
    execution_id: str
    state_version: int
    input: Mapping[str, Any]
    state: Mapping[str, Any]
    instructions: str = ""
    agent_metadata: Mapping[str, Any] = field(default_factory=dict)
    model: ModelAdapter | None = None
    tools: ToolRegistry | None = None
    memory: MemoryStore | None = None
    cancel_requested: bool = False


AgentNode = Callable[[AgentNodeContext], NodeResult]


@dataclass(frozen=True)
class AgentSpec:
    name: str
    instructions: str = ""
    description: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("agent name must be nonempty")
        if not isinstance(self.instructions, str) or not isinstance(self.description, str):
            raise TypeError("instructions and description must be strings")
        object.__setattr__(self, "metadata", _json_snapshot(self.metadata))


@dataclass(frozen=True)
class AgentResult:
    """Agent-facing wrapper that preserves the complete runtime result."""

    agent: AgentSpec
    run: RunResult

    @property
    def run_id(self) -> str:
        return self.run.run_id

    @property
    def status(self):
        return self.run.status

    @property
    def reason_code(self) -> str | None:
        return self.run.reason_code

    @property
    def stopped_at(self) -> str | None:
        return self.run.stopped_at

    @property
    def state(self) -> Mapping[str, Any]:
        return self.run.state

    @property
    def trace(self):
        return self.run.trace


class AgentBuilder:
    """Build an Agent graph while keeping application dependencies explicit."""

    def __init__(
        self,
        name: str,
        *,
        instructions: str = "",
        description: str = "",
        model: ModelAdapter | None = None,
        tools: ToolRegistry | None = None,
        memory: MemoryStore | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.spec = AgentSpec(name, instructions, description, metadata or {})
        self.model = model
        self.tools = tools
        self.memory = memory
        self._graph = Graph()

    def add_node(self, node_id: str, execute: AgentNode) -> "AgentBuilder":
        if not callable(execute):
            raise TypeError("agent node execute must be callable")

        def runtime_node(context: NodeContext) -> NodeResult:
            return execute(AgentNodeContext(
                run_id=context.run_id,
                node_id=context.node_id,
                execution_id=context.execution_id,
                state_version=context.state_version,
                input=context.input,
                state=context.state,
                instructions=self.spec.instructions,
                agent_metadata=self.spec.metadata,
                model=self.model,
                tools=self.tools,
                memory=self.memory,
                cancel_requested=context.cancel_requested,
            ))

        self._graph.add_node(node_id, runtime_node)
        return self

    def set_entry(self, node_id: str) -> "AgentBuilder":
        self._graph.set_entry(node_id)
        return self

    def add_edge(self, source: str, target: str, *, when: str | None = None) -> "AgentBuilder":
        self._graph.add_edge(source, target, when=when)
        return self

    def build(self, *, runtime: Runtime | None = None) -> "Agent":
        return Agent(
            self.spec,
            self._graph.compile(),
            model=self.model,
            tools=self.tools,
            memory=self.memory,
            runtime=runtime or Runtime(),
        )


class Agent:
    """Configured agent that executes a compiled graph."""

    def __init__(
        self,
        spec: AgentSpec,
        graph: Any,
        *,
        model: ModelAdapter | None = None,
        tools: ToolRegistry | None = None,
        memory: MemoryStore | None = None,
        runtime: Runtime | None = None,
    ) -> None:
        self.spec = spec
        self.graph = graph
        self.model = model
        self.tools = tools
        self.memory = memory
        self.runtime = runtime or Runtime()

    def run(self, input_data: Mapping[str, Any], *, run_id: str | None = None) -> AgentResult:
        if not isinstance(input_data, Mapping):
            raise TypeError("input_data must be a mapping")
        run = self.runtime.run(self.graph, input_data, run_id=run_id)
        return AgentResult(self.spec, run)


__all__ = [
    "Agent", "AgentBuilder", "AgentNode", "AgentNodeContext", "AgentResult",
    "AgentSpec", "InMemoryMemory", "MemoryStore", "ModelAdapter",
]

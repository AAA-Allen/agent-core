"""Domain-neutral agent workflow runtime."""

try:
    from importlib.metadata import version as _package_version

    __version__ = _package_version("agent-core")
except Exception:  # pragma: no cover - package not installed (repo checkout)
    __version__ = "0.2.0"

from .persistence import RunStore
from .runtime import RunContext, RunResult, RunStatus, Step, StepOutcome, StepStatus, TraceEvent, WorkflowRunner
from .tools import Tool, ToolRegistry, ToolResult, ToolStatus, graph_tool_node, tool_step
from .graph import CompiledGraph, Edge, Graph, Node, NodeContext
from .results import NodeResult, NodeStatus
from .runtime import Runtime
from .checkpoint import Checkpoint, CheckpointStore, FileCheckpointStore, InMemoryCheckpointStore
from .lifecycle import LifecycleEvent, LifecycleStatus, RunLifecycle
from .agent import (
    Agent, AgentBuilder, AgentNode, AgentNodeContext, AgentResult, AgentSpec,
    InMemoryMemory, MemoryStore, ModelAdapter,
)

__all__ = [
    "RunContext", "RunResult", "RunStatus", "Step", "StepOutcome", "StepStatus",
    "TraceEvent", "WorkflowRunner", "Tool", "ToolRegistry", "ToolResult", "ToolStatus", "tool_step", "graph_tool_node",
    "RunStore",
    "Graph", "CompiledGraph", "Node", "Edge", "NodeContext", "Runtime",
    "NodeResult", "NodeStatus",
    "Checkpoint", "CheckpointStore", "InMemoryCheckpointStore", "FileCheckpointStore",
    "LifecycleEvent", "LifecycleStatus", "RunLifecycle",
    "Agent", "AgentBuilder", "AgentNode", "AgentNodeContext", "AgentResult", "AgentSpec",
    "InMemoryMemory", "MemoryStore", "ModelAdapter",
    "__version__",
]

"""Domain-neutral tool registration and fail-closed invocation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping

from .runtime import RunContext, Step, StepOutcome, StepStatus, _json_snapshot
from .graph import NodeContext
from .results import NodeResult, NodeStatus


class ToolStatus(str, Enum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Tool:
    name: str
    invoke: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    required_capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool name must be nonempty")
        if not callable(self.invoke):
            raise TypeError("tool invoke must be callable")
        if not isinstance(self.required_capabilities, frozenset) or any(
            not isinstance(item, str) or not item.strip() for item in self.required_capabilities
        ):
            raise ValueError("required_capabilities must be a frozenset of nonempty strings")


@dataclass(frozen=True)
class ToolResult:
    name: str
    status: ToolStatus
    output: Mapping[str, Any] = field(default_factory=dict)
    reason_code: str | None = None
    error_type: str | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise TypeError("tool must be a Tool")
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def invoke(
        self, name: str, arguments: Mapping[str, Any], *,
        granted_capabilities: frozenset[str] = frozenset(),
    ) -> ToolResult:
        if not isinstance(name, str) or not name.strip():
            return ToolResult(str(name), ToolStatus.BLOCKED, reason_code="INVALID_TOOL_NAME")
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(name, ToolStatus.BLOCKED, reason_code="TOOL_NOT_FOUND")
        if (not isinstance(granted_capabilities, frozenset)
                or any(not isinstance(item, str) for item in granted_capabilities)
                or not tool.required_capabilities <= granted_capabilities):
            return ToolResult(name, ToolStatus.BLOCKED, reason_code="CAPABILITY_DENIED")
        if not isinstance(arguments, Mapping):
            return ToolResult(name, ToolStatus.BLOCKED, reason_code="INVALID_ARGUMENTS")
        try:
            safe_arguments = _json_snapshot(arguments)
        except (TypeError, ValueError, OverflowError):
            return ToolResult(name, ToolStatus.BLOCKED, reason_code="INVALID_ARGUMENTS")
        try:
            output = tool.invoke(safe_arguments)
        except Exception as exc:
            return ToolResult(name, ToolStatus.FAILED, reason_code="TOOL_EXCEPTION", error_type=type(exc).__name__)
        if not isinstance(output, Mapping):
            return ToolResult(name, ToolStatus.FAILED, reason_code="INVALID_TOOL_OUTPUT")
        try:
            safe_output = _json_snapshot(output)
        except (TypeError, ValueError, OverflowError):
            return ToolResult(name, ToolStatus.FAILED, reason_code="INVALID_TOOL_OUTPUT")
        return ToolResult(name, ToolStatus.COMPLETED, safe_output)


def tool_step(
    step_name: str, tool_name: str, registry: ToolRegistry,
    arguments: Callable[[RunContext], Mapping[str, Any]], *,
    granted_capabilities: frozenset[str] = frozenset(),
) -> Step:
    """Bind a registered tool to a workflow step with fixed capabilities."""
    if not isinstance(registry, ToolRegistry):
        raise TypeError("registry must be a ToolRegistry")
    if not callable(arguments):
        raise TypeError("arguments must be callable")

    def execute(context: RunContext) -> StepOutcome:
        result = registry.invoke(
            tool_name, arguments(context), granted_capabilities=granted_capabilities,
        )
        status = {
            ToolStatus.COMPLETED: StepStatus.COMPLETED,
            ToolStatus.BLOCKED: StepStatus.BLOCKED,
            ToolStatus.FAILED: StepStatus.FAILED,
        }[result.status]
        return StepOutcome(status, result.output, result.reason_code)

    return Step(step_name, execute)


def graph_tool_node(
    tool_name: str,
    registry: ToolRegistry,
    arguments: Callable[[NodeContext], Mapping[str, Any]],
    *,
    granted_capabilities: frozenset[str] = frozenset(),
) -> Callable[[NodeContext], NodeResult]:
    """Adapt a registered Tool into a v0.1 Graph node."""
    if not isinstance(registry, ToolRegistry):
        raise TypeError("registry must be a ToolRegistry")
    if not callable(arguments):
        raise TypeError("arguments must be callable")

    def execute(context: NodeContext) -> NodeResult:
        result = registry.invoke(
            tool_name, arguments(context), granted_capabilities=granted_capabilities,
        )
        status = {
            ToolStatus.COMPLETED: NodeStatus.SUCCEEDED,
            ToolStatus.BLOCKED: NodeStatus.BLOCKED,
            ToolStatus.FAILED: NodeStatus.FAILED,
        }[result.status]
        return NodeResult(
            status, result.output, reason_code=result.reason_code,
            output=result.output,
        )

    return execute

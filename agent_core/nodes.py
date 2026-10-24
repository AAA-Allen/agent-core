"""Declarative node factory library for config-assembled agents.

Each factory turns a validated parameter mapping into an ``AgentNode``. Nodes
are deterministic and fail-closed: bad parameters raise at assembly time, bad
run-time data becomes an explicit BLOCKED or FAILED node result, never an
exception inside shared state. No factory evaluates expressions, calls the
network, or imports application code; dynamic behavior enters through the
``tool`` node and a registered :class:`~agent_core.tools.ToolRegistry`.
"""

from __future__ import annotations

import re
from string import Formatter
from typing import Any, Callable, Mapping

from .agent import Agent, AgentBuilder, AgentNode, AgentNodeContext
from .results import NodeResult
from .tools import ToolRegistry

AgentNodeFactory = Callable[[Mapping[str, Any]], AgentNode]

_SPLIT_PATTERN = re.compile(r"[。;；!！?\n]+")
_FORMATTER = Formatter()


def _require_str(params: Mapping[str, Any], key: str, *, default: str | None = None) -> str:
    value = params.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"node param '{key}' must be a nonempty string")
    return value


def _require_route(params: Mapping[str, Any]) -> str | None:
    route = params.get("route")
    if route is None:
        return None
    if not isinstance(route, str) or not route.strip():
        raise ValueError("node param 'route' must be a nonempty string when provided")
    return route


def _format_from_state(template: str, state: Mapping[str, Any]) -> str:
    """Substitute ``{key}`` placeholders; attribute/index access is rejected."""
    output: list[str] = []
    for literal, field_name, _spec, _conversion in _FORMATTER.parse(template):
        output.append(literal)
        if field_name is None:
            continue
        if not field_name.isidentifier():
            raise KeyError(field_name)
        if field_name not in state:
            raise KeyError(field_name)
        output.append(str(state[field_name]))
    return "".join(output)


def validate_input_node(params: Mapping[str, Any]) -> AgentNode:
    """Require a nonempty string field; trim it into state on success."""
    field = _require_str(params, "field")
    blocked_reason = _require_str(params, "blocked_reason", default="EMPTY_INPUT")
    route = _require_route(params)

    def execute(ctx: AgentNodeContext) -> NodeResult:
        value = ctx.input.get(field)
        if not isinstance(value, str) or not value.strip():
            return NodeResult.blocked(blocked_reason)
        return NodeResult.success({field: value.strip()}, route=route)

    return execute


def split_steps_node(params: Mapping[str, Any]) -> AgentNode:
    """Split a text field into ordered plan entries at sentence boundaries."""
    field = _require_str(params, "field")
    into = _require_str(params, "into", default="plan")
    count_key = _require_str(params, "count_key", default="step_count")
    max_length = params.get("max_length", 60)
    if not isinstance(max_length, int) or isinstance(max_length, bool) or max_length <= 0:
        raise ValueError("node param 'max_length' must be a positive integer")
    route = _require_route(params)

    def execute(ctx: AgentNodeContext) -> NodeResult:
        text = ctx.state.get(field)
        if not isinstance(text, str) or not text.strip():
            return NodeResult.blocked("SPLIT_SOURCE_EMPTY")
        pieces = [piece.strip() for piece in _SPLIT_PATTERN.split(text) if piece.strip()]
        entries = pieces or [text.strip()]
        plan = [
            {"index": index + 1, "title": entry[:max_length]}
            for index, entry in enumerate(entries)
        ]
        return NodeResult.success({into: plan, count_key: len(plan)}, route=route)

    return execute


def template_node(params: Mapping[str, Any]) -> AgentNode:
    """Render state-driven strings into named state updates.

    ``updates`` maps state keys to ``{placeholder}`` templates; placeholders may
    only reference existing top-level state keys. Missing keys fail the node
    with ``TEMPLATE_KEY_MISSING`` instead of silently emitting partial text.
    """
    updates = params.get("updates")
    if not isinstance(updates, Mapping) or not updates:
        raise ValueError("node param 'updates' must be a nonempty mapping")
    if any(not isinstance(key, str) or not key.strip() for key in updates):
        raise ValueError("template update keys must be nonempty strings")
    if any(not isinstance(value, str) for value in updates.values()):
        raise ValueError("template update values must be strings")
    route = _require_route(params)

    def execute(ctx: AgentNodeContext) -> NodeResult:
        rendered: dict[str, Any] = {}
        for key, template in updates.items():
            try:
                rendered[key] = _format_from_state(template, ctx.state)
            except KeyError:
                return NodeResult.failed("TEMPLATE_KEY_MISSING")
        return NodeResult.success(rendered, route=route)

    return execute


def tool_node(params: Mapping[str, Any]) -> AgentNode:
    """Invoke a registered tool with literal or state-templated arguments."""
    name = _require_str(params, "name")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise ValueError("node param 'arguments' must be a mapping")
    output_key = _require_str(params, "output_key", default=name)
    route = _require_route(params)

    def execute(ctx: AgentNodeContext) -> NodeResult:
        if ctx.tools is None:
            return NodeResult.failed("TOOL_REGISTRY_MISSING")
        resolved: dict[str, Any] = {}
        for key, value in arguments.items():
            if isinstance(value, str):
                try:
                    resolved[key] = _format_from_state(value, ctx.state)
                except KeyError:
                    return NodeResult.failed("TOOL_ARGUMENT_MISSING")
            else:
                resolved[key] = value
        result = ctx.tools.invoke(name, resolved)
        if result.status.value == "COMPLETED":
            return NodeResult.success({output_key: dict(result.output)}, route=route)
        if result.status.value == "BLOCKED":
            return NodeResult.blocked(result.reason_code or "TOOL_BLOCKED")
        return NodeResult.failed(result.reason_code or "TOOL_FAILED")

    return execute


def route_by_value_node(params: Mapping[str, Any]) -> AgentNode:
    """Choose the outgoing route from a state value's mapping."""
    field = _require_str(params, "field")
    routes = params.get("routes")
    if not isinstance(routes, Mapping) or not routes:
        raise ValueError("node param 'routes' must be a nonempty mapping")
    if any(not isinstance(key, str) or not isinstance(value, str) or not value.strip()
           for key, value in routes.items()):
        raise ValueError("route entries must map nonempty strings to nonempty strings")
    default_route = params.get("default_route")
    if default_route is not None and (not isinstance(default_route, str) or not default_route.strip()):
        raise ValueError("node param 'default_route' must be a nonempty string when provided")

    def execute(ctx: AgentNodeContext) -> NodeResult:
        value = ctx.state.get(field)
        key = value if isinstance(value, str) else None
        route = routes.get(key, default_route) if key is not None else default_route
        if route is None:
            return NodeResult.blocked("ROUTE_UNRESOLVED")
        return NodeResult.success({}, route=route)

    return execute


NODE_FACTORIES: dict[str, AgentNodeFactory] = {
    "validate_input": validate_input_node,
    "split_steps": split_steps_node,
    "template": template_node,
    "tool": tool_node,
    "route_by_value": route_by_value_node,
}


def register_node_factory(node_type: str, factory: AgentNodeFactory) -> None:
    """Extend the declarative library with an application node type."""
    if not isinstance(node_type, str) or not node_type.strip():
        raise ValueError("node type must be a nonempty string")
    if not callable(factory):
        raise TypeError("node factory must be callable")
    if node_type in NODE_FACTORIES:
        raise ValueError(f"node factory already registered: {node_type}")
    NODE_FACTORIES[node_type] = factory


def _validate_config(config: Mapping[str, Any]) -> None:
    if not isinstance(config, Mapping):
        raise TypeError("agent config must be a mapping")
    name = config.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("agent config 'name' must be a nonempty string")
    for key in ("instructions", "description"):
        value = config.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"agent config '{key}' must be a string")
    nodes = config.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("agent config 'nodes' must be a nonempty list")
    seen: set[str] = set()
    for node in nodes:
        if not isinstance(node, Mapping):
            raise ValueError("each node must be a mapping")
        node_id = node.get("id")
        node_type = node.get("type")
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("node 'id' must be a nonempty string")
        if node_id in seen:
            raise ValueError(f"duplicate node id: {node_id}")
        seen.add(node_id)
        if not isinstance(node_type, str) or node_type not in NODE_FACTORIES:
            raise ValueError(f"unknown node type: {node_type!r}")
        node_params = node.get("params", {})
        if not isinstance(node_params, Mapping):
            raise ValueError(f"node '{node_id}' params must be a mapping")
    entry = config.get("entry")
    if not isinstance(entry, str) or entry not in seen:
        raise ValueError("agent config 'entry' must name a registered node")
    edges = config.get("edges", [])
    if not isinstance(edges, list):
        raise ValueError("agent config 'edges' must be a list")
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise ValueError("each edge must be a mapping")
        source, target = edge.get("source"), edge.get("target")
        if not isinstance(source, str) or source not in seen:
            raise ValueError(f"edge source is not a registered node: {source!r}")
        if not isinstance(target, str) or target not in seen:
            raise ValueError(f"edge target is not a registered node: {target!r}")
        when = edge.get("when")
        if when is not None and (not isinstance(when, str) or not when.strip()):
            raise ValueError("edge 'when' must be a nonempty string when provided")


def build_agent_from_config(
    config: Mapping[str, Any],
    *,
    runtime: Any,
    tools: ToolRegistry | None = None,
    memory: Any = None,
) -> Agent:
    """Assemble an :class:`Agent` from a validated declarative config."""
    _validate_config(config)
    builder = AgentBuilder(
        config["name"],
        instructions=config.get("instructions", ""),
        description=config.get("description", ""),
        metadata=config.get("metadata", {}),
        tools=tools,
        memory=memory,
    )
    for node in config["nodes"]:
        factory = NODE_FACTORIES[node["type"]]
        node_fn = factory(dict(node.get("params", {})))
        builder.add_node(node["id"], node_fn)
    builder.set_entry(config["entry"])
    for edge in config.get("edges", []):
        builder.add_edge(edge["source"], edge["target"], when=edge.get("when"))
    return builder.build(runtime=runtime)


__all__ = [
    "NODE_FACTORIES",
    "build_agent_from_config",
    "register_node_factory",
    "route_by_value_node",
    "split_steps_node",
    "template_node",
    "tool_node",
    "validate_input_node",
]

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
    """Invoke a registered tool with literal or state-templated arguments.

    ``capabilities`` declares the capability names this agent grants the
    tool; a tool whose ``required_capabilities`` is not covered blocks with
    ``CAPABILITY_DENIED`` — domain capabilities (e.g. medical inference)
    must be declared explicitly, never granted implicitly.
    """
    name = _require_str(params, "name")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, Mapping):
        raise ValueError("node param 'arguments' must be a mapping")
    capabilities = params.get("capabilities", [])
    if not isinstance(capabilities, list) or any(
        not isinstance(item, str) or not item.strip() for item in capabilities
    ):
        raise ValueError("node param 'capabilities' must be a list of nonempty strings")
    granted = frozenset(capabilities)
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
        result = ctx.tools.invoke(name, resolved, granted_capabilities=granted)
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


def subagent_node(params: Mapping[str, Any], *, resolver: Callable[[str, Any], Any]) -> AgentNode:
    """Delegate to another registered agent as a nested run.

    The sub-run gets its own run_id, trace and lifecycle; the parent node
    propagates the sub-run's terminal state (COMPLETED → success, BLOCKED →
    blocked, FAILED/CANCELED → failed) so the fail-closed boundary stays
    auditable. ``input`` maps sub-run input keys to literal or state-templated
    values; ``summary_into`` hoists the sub-run's ``state["summary"]`` to a
    top-level parent key. Cancellation of the parent run does not interrupt an
    in-flight sub-run (it is an independent run); agents registered only in
    code cannot be inspected for cycles, so config-level cycle checks are the
    caller's responsibility for those.
    """
    name = _require_str(params, "agent")
    input_spec = params.get("input", {})
    if not isinstance(input_spec, Mapping):
        raise ValueError("node param 'input' must be a mapping")
    output_key = _require_str(params, "output_key", default="sub_result")
    summary_into = params.get("summary_into")
    if summary_into is not None and (not isinstance(summary_into, str) or not summary_into.strip()):
        raise ValueError("node param 'summary_into' must be a nonempty string when provided")
    route = _require_route(params)
    if not callable(resolver):
        raise ValueError("subagent nodes require a callable agent resolver")

    def execute(ctx: AgentNodeContext) -> NodeResult:
        agent = resolver(name, ctx.memory)
        if agent is None:
            return NodeResult.blocked("SUBAGENT_NOT_FOUND")
        arguments: dict[str, Any] = {}
        for key, value in input_spec.items():
            if isinstance(value, str):
                try:
                    arguments[key] = _format_from_state(value, ctx.state)
                except KeyError:
                    return NodeResult.failed("SUBAGENT_INPUT_MISSING")
            else:
                arguments[key] = value
        try:
            result = agent.run(arguments)
        except TypeError:
            return NodeResult.failed("SUBAGENT_INPUT_INVALID")
        status_value = result.status.value
        if status_value == "COMPLETED":
            updates: dict[str, Any] = {
                output_key: {"run_id": result.run_id, "status": "COMPLETED", "state": dict(result.state)},
            }
            if summary_into:
                summary = result.state.get("summary") if isinstance(result.state, Mapping) else None
                if isinstance(summary, str):
                    updates[summary_into] = summary
            return NodeResult.success(updates, route=route)
        if status_value == "BLOCKED":
            return NodeResult.blocked(result.reason_code or "SUBAGENT_BLOCKED")
        return NodeResult.failed(result.reason_code or f"SUBAGENT_{status_value}")

    return execute


def llm_node(params: Mapping[str, Any]) -> AgentNode:
    """Ask the injected model a prompt rendered from state.

    ``prompt`` and optional ``system`` are ``{state_key}`` templates. The
    reply content is written to the state key named by ``into``. Fails
    closed: no model bound → BLOCKED ``MODEL_NOT_CONFIGURED``; a transport
    or protocol failure → FAILED with the adapter's reason code. Prompts and
    replies are plain strings; structured output belongs in a tool node.

    If the adapter implements the optional ``complete_stream(messages,
    on_delta)`` convention, it is used instead of ``complete`` and every
    delta is published to the process token bus (keyed by run id) so HTTP
    consumers can render tokens live; the assembled reply is identical.
    """
    prompt = _require_str(params, "prompt")
    into = _require_str(params, "into", default="reply")
    system = params.get("system")
    if system is not None and not isinstance(system, str):
        raise ValueError("node param 'system' must be a string when provided")
    route = _require_route(params)

    def execute(ctx: AgentNodeContext) -> NodeResult:
        if ctx.model is None:
            return NodeResult.blocked("MODEL_NOT_CONFIGURED")
        try:
            rendered_prompt = _format_from_state(prompt, ctx.state)
            rendered_system = _format_from_state(system, ctx.state) if system else None
        except KeyError:
            return NodeResult.failed("PROMPT_KEY_MISSING")
        messages: list[dict[str, str]] = []
        if rendered_system:
            messages.append({"role": "system", "content": rendered_system})
        messages.append({"role": "user", "content": rendered_prompt})
        stream_fn = getattr(ctx.model, "complete_stream", None)
        if callable(stream_fn):
            from .streaming import default_bus

            bus = default_bus()
            bus.open(ctx.run_id)
            try:
                reply = stream_fn(
                    messages,
                    on_delta=lambda text: bus.publish(ctx.run_id, ctx.node_id, text),
                )
            except Exception as exc:
                reason = getattr(exc, "reason_code", None)
                if isinstance(reason, str) and reason.strip():
                    return NodeResult.failed(reason)
                return NodeResult.failed("MODEL_REQUEST_FAILED")
            finally:
                bus.close(ctx.run_id)
        else:
            try:
                reply = ctx.model.complete(messages)
            except Exception as exc:
                reason = getattr(exc, "reason_code", None)
                if isinstance(reason, str) and reason.strip():
                    return NodeResult.failed(reason)
                return NodeResult.failed("MODEL_REQUEST_FAILED")
        content = reply.get("content") if isinstance(reply, Mapping) else None
        if not isinstance(content, str):
            return NodeResult.failed("MODEL_INVALID_REPLY")
        return NodeResult.success({into: content}, route=route)

    return execute


NODE_FACTORIES: dict[str, AgentNodeFactory] = {
    "validate_input": validate_input_node,
    "split_steps": split_steps_node,
    "template": template_node,
    "tool": tool_node,
    "route_by_value": route_by_value_node,
    "llm": llm_node,
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
        if not isinstance(node_type, str) or (
            node_type not in NODE_FACTORIES and node_type != "subagent"
        ):
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
    model: Any = None,
    agent_resolver: Callable[[str, Any], Any] | None = None,
) -> Agent:
    """Assemble an :class:`Agent` from a validated declarative config.

    ``agent_resolver(name, memory)`` is required only when the config uses
    ``subagent`` nodes; it builds the delegate agent for each sub-run.
    ``model`` is bound into the agent context for ``llm`` nodes and any
    custom node that wants it.
    """
    _validate_config(config)
    builder = AgentBuilder(
        config["name"],
        instructions=config.get("instructions", ""),
        description=config.get("description", ""),
        metadata=config.get("metadata", {}),
        tools=tools,
        memory=memory,
        model=model,
    )
    for node in config["nodes"]:
        if node["type"] == "subagent":
            if agent_resolver is None:
                raise ValueError(f"subagent node '{node['id']}' requires an agent_resolver")
            node_fn = subagent_node(dict(node.get("params", {})), resolver=agent_resolver)
        else:
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
    "subagent_node",
    "template_node",
    "tool_node",
    "validate_input_node",
]

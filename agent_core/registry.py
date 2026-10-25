"""Agent registry, session memory, and declarative config loading."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from .agent import Agent, InMemoryMemory
from .nodes import build_agent_from_config
from .runtime import Runtime, _validate_run_id
from .tools import Tool, ToolRegistry


class AgentRegistry:
    """Process-local mapping of agent names to configured agents."""

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._factories: dict[str, Callable[..., Agent]] = {}
        self._lock = threading.RLock()

    def register(self, agent: Agent) -> None:
        if not isinstance(agent, Agent):
            raise TypeError("agent must be an Agent instance")
        with self._lock:
            if agent.spec.name in self._agents or agent.spec.name in self._factories:
                raise ValueError(f"agent already registered: {agent.spec.name}")
            self._agents[agent.spec.name] = agent
            self._factories[agent.spec.name] = lambda memory=None, agent=agent: agent

    def register_factory(self, name: str, factory: Callable[..., Agent]) -> None:
        """Register a buildable agent; the factory receives ``memory=``."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("agent name must be a nonempty string")
        if not callable(factory):
            raise TypeError("agent factory must be callable")
        with self._lock:
            if name in self._agents or name in self._factories:
                raise ValueError(f"agent already registered: {name}")
            master = factory(memory=None)
            if not isinstance(master, Agent):
                raise TypeError("agent factory must return an Agent instance")
            if master.spec.name != name:
                raise ValueError(f"agent factory builds a different name: {master.spec.name!r} != {name!r}")
            self._agents[name] = master
            self._factories[name] = factory

    def unregister(self, name: str) -> bool:
        """Remove an agent registration; returns whether it existed."""
        if not isinstance(name, str):
            return False
        with self._lock:
            existed = name in self._agents or name in self._factories
            self._agents.pop(name, None)
            self._factories.pop(name, None)
            return existed

    def build(self, name: str, *, memory: Any = None) -> Agent | None:
        """Build an agent instance, optionally bound to session memory."""
        if not isinstance(name, str):
            return None
        with self._lock:
            factory = self._factories.get(name)
        if factory is None:
            return self.get(name)
        try:
            return factory(memory=memory)
        except TypeError:
            return factory()

    def get(self, name: str) -> Agent | None:
        if not isinstance(name, str):
            return None
        with self._lock:
            return self._agents.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._agents)

    def descriptions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "name": agent.spec.name,
                    "instructions": agent.spec.instructions,
                    "description": agent.spec.description,
                    "metadata": dict(agent.spec.metadata),
                    "graph": agent.describe(),
                }
                for agent in (self._agents[name] for name in sorted(self._agents))
            ]


class SessionManager:
    """Per-session memory instances shared by an agent's runs."""

    def __init__(self) -> None:
        self._memories: dict[str, InMemoryMemory] = {}
        self._lock = threading.RLock()

    def memory_for(self, session_id: str) -> InMemoryMemory:
        _validate_run_id(session_id)
        with self._lock:
            memory = self._memories.get(session_id)
            if memory is None:
                memory = InMemoryMemory()
                self._memories[session_id] = memory
            return memory

    def snapshot(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            memory = self._memories.get(session_id)
        return None if memory is None else memory.snapshot()

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(self._memories)

    def clear(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._memories:
                del self._memories[session_id]
                return True
            return False



def agent_resolver(registry: AgentRegistry) -> Callable[[str, Any], Agent | None]:
    """Build delegate agents on demand, sharing the caller's session memory."""
    return lambda name, memory=None: registry.build(name, memory=memory)


def _config_dependencies(configs: list[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Map config agent names to their in-config subagent dependencies."""
    dependencies: dict[str, list[str]] = {}
    for config in configs:
        name = config["name"]
        deps: list[str] = []
        for node in config.get("nodes", []):
            if not isinstance(node, Mapping) or node.get("type") != "subagent":
                continue
            params = node.get("params")
            target = params.get("agent") if isinstance(params, Mapping) else None
            if isinstance(target, str) and target.strip():
                deps.append(target)
        dependencies[name] = deps
    return dependencies


def _reject_dependency_cycles(dependencies: dict[str, list[str]]) -> None:
    visiting: set[str] = set()
    done: set[str] = set()
    stack: list[str] = []

    def visit(name: str) -> None:
        if name in done:
            return
        if name in visiting:
            cycle = stack[stack.index(name):] + [name]
            raise ValueError("agent dependency cycle: " + " -> ".join(cycle))
        visiting.add(name)
        stack.append(name)
        for dep in dependencies.get(name, []):
            if dep in dependencies:
                visit(dep)
        stack.pop()
        visiting.discard(name)
        done.add(name)

    for name in dependencies:
        visit(name)


def load_agents_from_config(
    path: str | Path,
    *,
    runtime: Runtime,
    tools: Any = None,
    model: Any = None,
    agent_resolver: Callable[[str, Any], Agent | None] | None = None,
) -> list[Agent]:
    """Build agents from a JSON config file (single config or ``agents`` list)."""
    configs = _read_agent_configs(path)
    if tools is None:
        tools = default_tools()
    return [
        build_agent_from_config(
            config, runtime=runtime, tools=tools, model=model,
            agent_resolver=agent_resolver,
        )
        for config in configs
    ]


def register_agents_from_config(
    path: str | Path,
    registry: AgentRegistry,
    *,
    runtime: Runtime,
    tools: Any = None,
    model: Any = None,
) -> list[str]:
    """Register config agents as factories so sessions can bind memory.

    Rejects subagent dependency cycles among the loaded configs at startup.
    """
    configs = _read_agent_configs(path)
    _reject_dependency_cycles(_config_dependencies(configs))
    if tools is None:
        tools = default_tools()
    resolver = agent_resolver(registry)
    for config in configs:
        config = dict(config)
        registry.register_factory(
            config["name"],
            lambda memory=None, config=config: build_agent_from_config(
                config, runtime=runtime, tools=tools, memory=memory,
                model=model, agent_resolver=resolver,
            ),
        )
    return [config["name"] for config in configs]


def _read_agent_configs(path: str | Path) -> list[Mapping[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, Mapping) and isinstance(payload.get("agents"), list):
        return payload["agents"]
    if isinstance(payload, Mapping):
        return [payload]
    raise ValueError("agent config file must be an object or {'agents': [...]}")



def default_tools() -> Any:
    from .tools import Tool, ToolRegistry

    tools = ToolRegistry()
    tools.register(
        Tool("text_stats", lambda args: {"chars": len(args["task"]), "words": len(args["task"].split())})
    )
    return tools


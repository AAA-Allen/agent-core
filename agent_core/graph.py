"""Graph definition and read-only node context for the v0.1 runtime."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .results import NodeResult
from .runtime import _json_snapshot


NodeCallable = Callable[["NodeContext"], NodeResult]


@dataclass(frozen=True)
class NodeContext:
    run_id: str
    node_id: str
    execution_id: str
    state_version: int
    _input: Mapping[str, Any]
    _state: Mapping[str, Any]

    @property
    def input(self) -> Mapping[str, Any]:
        return _readonly_snapshot(self._input)

    @property
    def state(self) -> Mapping[str, Any]:
        return _readonly_snapshot(self._state)


@dataclass(frozen=True)
class Node:
    node_id: str
    execute: NodeCallable


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    when: str | None = None


@dataclass(frozen=True)
class CompiledGraph:
    nodes: Mapping[str, Node]
    edges: tuple[Edge, ...]
    entry: str

    def outgoing(self, node_id: str) -> tuple[Edge, ...]:
        return tuple(edge for edge in self.edges if edge.source == node_id)


class Graph:
    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self._entry: str | None = None

    def add_node(self, node_id: str, execute: NodeCallable) -> "Graph":
        _validate_identifier(node_id, "node_id")
        if not callable(execute):
            raise TypeError("node execute must be callable")
        if node_id in self._nodes:
            raise ValueError(f"node already registered: {node_id}")
        self._nodes[node_id] = Node(node_id, execute)
        return self

    def set_entry(self, node_id: str) -> "Graph":
        _validate_identifier(node_id, "entry")
        self._entry = node_id
        return self

    def add_edge(
        self, source: str, target: str, *, when: str | None = None
    ) -> "Graph":
        _validate_identifier(source, "source")
        _validate_identifier(target, "target")
        if when is not None:
            _validate_identifier(when, "when")
        edge = Edge(source, target, when)
        if edge in self._edges:
            raise ValueError("edge already registered")
        self._edges.append(edge)
        return self

    def compile(self) -> CompiledGraph:
        if not self._nodes:
            raise ValueError("graph needs at least one node")
        if self._entry is None or self._entry not in self._nodes:
            raise ValueError("graph needs a valid entry node")
        for edge in self._edges:
            if edge.source not in self._nodes or edge.target not in self._nodes:
                raise ValueError("edge references an unknown node")
        for node_id in self._nodes:
            defaults = [edge for edge in self._edges
                        if edge.source == node_id and edge.when is None]
            if len(defaults) > 1:
                raise ValueError(f"multiple default edges from node: {node_id}")

        reachable = {self._entry}
        changed = True
        while changed:
            changed = False
            for edge in self._edges:
                if edge.source in reachable and edge.target not in reachable:
                    reachable.add(edge.target)
                    changed = True
        if reachable != set(self._nodes):
            missing = sorted(set(self._nodes) - reachable)
            raise ValueError(f"unreachable nodes: {', '.join(missing)}")

        return CompiledGraph(
            MappingProxyType(dict(self._nodes)), tuple(self._edges), self._entry
        )


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")


def _readonly_snapshot(value: Mapping[str, Any]) -> Mapping[str, Any]:
    snapshot = _json_snapshot(value)
    return MappingProxyType(snapshot)

"""A deliberately small Agent core.

The package only owns the conversation loop. Applications provide a model
adapter with ``complete`` or ``complete_with_tools`` and ordinary Python
functions as tools. Persistence, HTTP, graphs and scheduling stay outside.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence


class AgentError(RuntimeError):
    """Raised when a model or tool response violates the small core contract."""


class Model(Protocol):
    def complete(self, messages: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class Tool:
    """A named Python function exposed to the model."""

    name: str
    function: Callable[..., Any]
    description: str = ""
    parameters: Mapping[str, Any] | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters or {
                    "type": "object", "properties": {}, "additionalProperties": True,
                }),
            },
        }


class ToolRegistry:
    def __init__(self, tools: Sequence[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.add(tool)

    def add(self, tool: Tool) -> "ToolRegistry":
        if not isinstance(tool, Tool) or not tool.name.strip():
            raise ValueError("tool must have a nonempty name")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        if not callable(tool.function):
            raise TypeError("tool function must be callable")
        self._tools[tool.name] = tool
        return self

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def call(self, name: str, arguments: Any) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise AgentError(f"unknown tool: {name}")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise AgentError(f"invalid arguments for tool: {name}") from exc
        if not isinstance(arguments, Mapping):
            raise AgentError(f"tool arguments must be an object: {name}")
        try:
            return tool.function(**dict(arguments))
        except TypeError:
            raise
        except Exception as exc:
            raise AgentError(f"tool failed: {name}") from exc


@dataclass(frozen=True)
class AgentResponse:
    """Final response plus the messages used to produce it."""

    content: str
    messages: tuple[Mapping[str, Any], ...]

    def __str__(self) -> str:
        return self.content


class Agent:
    """Minimal conversation and model-driven tool loop."""

    def __init__(
        self,
        model: Model,
        *,
        system: str = "",
        tools: Sequence[Tool] = (),
        max_rounds: int = 8,
        history: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        if not callable(getattr(model, "complete", None)) and not callable(
            getattr(model, "complete_with_tools", None)
        ):
            raise TypeError("model must provide complete or complete_with_tools")
        if max_rounds <= 0:
            raise ValueError("max_rounds must be positive")
        self.model = model
        self.tools = ToolRegistry(tools)
        self.max_rounds = max_rounds
        self._messages: list[dict[str, Any]] = []
        if system:
            self._messages.append({"role": "system", "content": system})
        if history:
            self._messages.extend(copy.deepcopy(list(history)))

    @property
    def history(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(copy.deepcopy(self._messages))

    def reset(self) -> None:
        system = next((m for m in self._messages if m.get("role") == "system"), None)
        self._messages[:] = [] if system is None else [system]

    def run(self, user_input: str) -> AgentResponse:
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("user_input must be a nonempty string")
        self._messages.append({"role": "user", "content": user_input})
        for _ in range(self.max_rounds):
            response = self._complete()
            tool_calls = response.get("tool_calls") or []
            content = response.get("content") or ""
            if not isinstance(content, str):
                raise AgentError("model content must be a string")
            if not tool_calls:
                self._messages.append({"role": "assistant", "content": content})
                return AgentResponse(content, self.history)
            assistant_message = {"role": "assistant", "content": content, "tool_calls": tool_calls}
            self._messages.append(assistant_message)
            for call in tool_calls:
                name, call_id, arguments = self._parse_call(call)
                try:
                    output = self.tools.call(name, arguments)
                except Exception as exc:
                    output = {"error": str(exc)}
                self._messages.append({
                    "role": "tool", "tool_call_id": call_id, "name": name,
                    "content": _to_content(output),
                })
        raise AgentError("maximum tool rounds exceeded")

    def _complete(self) -> Mapping[str, Any]:
        messages = self.history
        complete_with_tools = getattr(self.model, "complete_with_tools", None)
        if callable(complete_with_tools) and self.tools.schemas():
            response = complete_with_tools(messages, self.tools.schemas())
        else:
            response = self.model.complete(messages)
        if not isinstance(response, Mapping):
            raise AgentError("model response must be an object")
        return response

    @staticmethod
    def _parse_call(call: Any) -> tuple[str, str, Any]:
        if not isinstance(call, Mapping):
            raise AgentError("invalid tool call")
        function = call.get("function", call)
        if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
            raise AgentError("invalid tool call function")
        return (
            function["name"],
            str(call.get("id", function["name"])),
            function.get("arguments", {}),
        )


def _to_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


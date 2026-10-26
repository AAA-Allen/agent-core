"""Application-facing model adapters.

The runtime itself stays model-agnostic: nodes decide when model work is part
of their workflow and reach the adapter through ``AgentNodeContext.model``.
This module ships one concrete adapter over the OpenAI-compatible chat
completions protocol, implemented with the standard library only. It makes
exactly one HTTP request per ``complete`` call and never retries; failures
raise :class:`ModelRequestError` so nodes can map them onto explicit node
results instead of leaking raw exceptions into shared state.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ModelRequestError(Exception):
    """A model call failed at the transport or protocol layer.

    ``reason_code`` is a stable machine-readable cause; the message never
    contains the request payload or API key.
    """

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _http_error_reason(code: int) -> str:
    # Local proxy failures commonly surface as gateway errors even when the
    # target port has no listener; expose those as transport reachability.
    return "MODEL_UNREACHABLE" if code in (502, 503, 504) else "MODEL_HTTP_ERROR"


class OpenAICompatibleModel:
    """Chat-completions adapter for any OpenAI-compatible endpoint.

    Configuration is caller-owned (environment variables, config files, or
    explicit arguments); nothing is read implicitly. Exactly one HTTP request
    per ``complete`` call, never retried; failures raise
    :class:`ModelRequestError` so nodes can map them onto explicit node
    results instead of leaking raw exceptions into shared state.
    """

    _PATH = "/chat/completions"
    _MAX_RESPONSE_BYTES = 10_000_000

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not isinstance(base_url, str) or not re.match(r"^https?://\S+$", base_url.strip()):
            raise ValueError("base_url must be an http(s) URL")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a nonempty string")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a nonempty string")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) \
                or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = float(timeout_seconds)

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        if not isinstance(messages, Sequence) or not messages:
            raise ValueError("messages must be a nonempty sequence")
        for message in messages:
            if not isinstance(message, Mapping) or not isinstance(message.get("role"), str) \
                    or not isinstance(message.get("content"), str):
                raise ValueError("each message needs string 'role' and 'content'")
        payload = json.dumps({
            "model": self.model,
            "messages": [dict(message) for message in messages],
        }, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(
            self.base_url + self._PATH,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read(self._MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            try:
                code = exc.code
            finally:
                exc.close()
            raise ModelRequestError(
                _http_error_reason(code), f"model endpoint returned HTTP {code}"
            ) from None
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            code = "MODEL_TIMEOUT" if isinstance(reason, TimeoutError) else "MODEL_UNREACHABLE"
            raise ModelRequestError(code, f"model endpoint unreachable: {type(reason).__name__}") from None
        except OSError as exc:
            raise ModelRequestError("MODEL_TRANSPORT_ERROR", f"model transport failed: {type(exc).__name__}") from None
        if len(body) > self._MAX_RESPONSE_BYTES:
            raise ModelRequestError("MODEL_RESPONSE_TOO_LARGE", "model response exceeded size limit")
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ModelRequestError("MODEL_INVALID_RESPONSE", "model response is not valid JSON") from None
        content, usage = self._extract(parsed)
        return {"content": content, "model": self.model, "usage": usage}

    def complete_with_tools(
        self,
        messages: Sequence[Mapping[str, Any]],
        tool_schemas: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Tool-use variant: let the model request tool calls.

        ``tool_schemas`` are OpenAI-style function definitions
        (``{"name", "description", "parameters"}``). Returns either
        ``{"content": str}`` (final answer, tool_calls absent) or
        ``{"tool_calls": [{"id", "name", "arguments": dict}]}``. Optional
        adapter convention — callers must handle models without it.
        """
        if not isinstance(messages, Sequence) or not messages:
            raise ValueError("messages must be a nonempty sequence")
        if not isinstance(tool_schemas, Sequence) or not tool_schemas:
            raise ValueError("tool_schemas must be a nonempty sequence")
        wire_tools = []
        for schema in tool_schemas:
            if not isinstance(schema, Mapping) or not isinstance(schema.get("name"), str) \
                    or not schema["name"].strip():
                raise ValueError("each tool schema needs a nonempty string 'name'")
            function = {"name": schema["name"]}
            if "description" in schema:
                function["description"] = schema["description"]
            if "parameters" in schema:
                function["parameters"] = schema["parameters"]
            wire_tools.append({"type": "function", "function": function})
        payload = json.dumps({
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "tools": wire_tools,
            "tool_choice": "auto",
        }, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(
            self.base_url + self._PATH,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                code = exc.code
            finally:
                exc.close()
            raise ModelRequestError(
                _http_error_reason(code), f"model endpoint returned HTTP {code}"
            ) from None
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            code = "MODEL_TIMEOUT" if isinstance(reason, TimeoutError) else "MODEL_UNREACHABLE"
            raise ModelRequestError(code, f"model endpoint unreachable: {type(reason).__name__}") from None
        except OSError as exc:
            raise ModelRequestError("MODEL_TRANSPORT_ERROR", f"model transport failed: {type(exc).__name__}") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ModelRequestError("MODEL_INVALID_RESPONSE", "model response is not valid JSON") from None

        choices = parsed.get("choices") if isinstance(parsed, Mapping) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ModelRequestError("MODEL_INVALID_RESPONSE", "model response has no choices")
        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            raise ModelRequestError("MODEL_INVALID_RESPONSE", "model choice has no message")
        tool_calls = message.get("tool_calls")
        if tool_calls is None:
            content = message.get("content")
            if not isinstance(content, str):
                raise ModelRequestError("MODEL_INVALID_RESPONSE", "model message has no content")
            return {"content": content, "model": self.model}
        if not isinstance(tool_calls, list) or not tool_calls:
            raise ModelRequestError("MODEL_INVALID_RESPONSE", "tool_calls must be a nonempty list")
        parsed_calls = []
        for call in tool_calls:
            if not isinstance(call, Mapping):
                raise ModelRequestError("MODEL_INVALID_RESPONSE", "tool call must be an object")
            call_id = call.get("id")
            function = call.get("function")
            if not isinstance(call_id, str) or not isinstance(function, Mapping) \
                    or not isinstance(function.get("name"), str):
                raise ModelRequestError("MODEL_INVALID_RESPONSE", "tool call needs id and function name")
            raw_arguments = function.get("arguments", "{}")
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError:
                raise ModelRequestError("MODEL_INVALID_RESPONSE", "tool call arguments is not valid JSON") from None
            if not isinstance(arguments, Mapping):
                raise ModelRequestError("MODEL_INVALID_RESPONSE", "tool call arguments must be an object")
            parsed_calls.append({"id": call_id, "name": function["name"], "arguments": dict(arguments)})
        return {"tool_calls": parsed_calls, "model": self.model}

    def complete_stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        on_delta: Callable[[str], None] | None = None,
    ) -> Mapping[str, Any]:
        """Streaming variant: invokes ``on_delta(text)`` per content chunk.

        Uses the endpoint's SSE streaming mode and returns the same mapping
        contract as :meth:`complete` (content assembled from deltas). Errors
        carry the same reason codes. This method is an optional adapter
        convention — nodes fall back to ``complete`` when it is absent.
        """
        if not isinstance(messages, Sequence) or not messages:
            raise ValueError("messages must be a nonempty sequence")
        for message in messages:
            if not isinstance(message, Mapping) or not isinstance(message.get("role"), str) \
                    or not isinstance(message.get("content"), str):
                raise ValueError("each message needs string 'role' and 'content'")
        payload = json.dumps({
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "stream": True,
        }, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(
            self.base_url + self._PATH,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            response = urlopen(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            try:
                code = exc.code
            finally:
                exc.close()
            raise ModelRequestError(
                _http_error_reason(code), f"model endpoint returned HTTP {code}"
            ) from None
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            code = "MODEL_TIMEOUT" if isinstance(reason, TimeoutError) else "MODEL_UNREACHABLE"
            raise ModelRequestError(code, f"model endpoint unreachable: {type(reason).__name__}") from None
        except OSError as exc:
            raise ModelRequestError("MODEL_TRANSPORT_ERROR", f"model transport failed: {type(exc).__name__}") from None

        parts: list[str] = []
        usage: dict[str, Any] = {}
        try:
            with response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        raise ModelRequestError("MODEL_INVALID_RESPONSE", "stream chunk is not valid JSON") from None
                    if not isinstance(chunk, Mapping):
                        raise ModelRequestError("MODEL_INVALID_RESPONSE", "stream chunk must be an object")
                    usage_update = chunk.get("usage")
                    if isinstance(usage_update, Mapping):
                        usage = dict(usage_update)
                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    first = choices[0]
                    if not isinstance(first, Mapping):
                        raise ModelRequestError("MODEL_INVALID_RESPONSE", "stream choice must be an object")
                    delta = first.get("delta")
                    if isinstance(delta, Mapping) and isinstance(delta.get("content"), str):
                        parts.append(delta["content"])
                        if on_delta is not None:
                            on_delta(delta["content"])
        except (UnicodeDecodeError, OSError) as exc:
            raise ModelRequestError("MODEL_TRANSPORT_ERROR", f"stream read failed: {type(exc).__name__}") from None
        return {"content": "".join(parts), "model": self.model, "usage": usage}

    @staticmethod
    def _extract(parsed: Any) -> tuple[str, Mapping[str, Any]]:
        if not isinstance(parsed, Mapping):
            raise ModelRequestError("MODEL_INVALID_RESPONSE", "model response must be an object")
        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelRequestError("MODEL_INVALID_RESPONSE", "model response has no choices")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ModelRequestError("MODEL_INVALID_RESPONSE", "model choice must be an object")
        message = first.get("message")
        if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
            raise ModelRequestError("MODEL_INVALID_RESPONSE", "model choice has no message content")
        usage = parsed.get("usage")
        if usage is not None and not isinstance(usage, Mapping):
            raise ModelRequestError("MODEL_INVALID_RESPONSE", "model usage must be an object")
        return message["content"], dict(usage) if isinstance(usage, Mapping) else {}


def model_from_environment(environ: Mapping[str, str] | None = None) -> OpenAICompatibleModel | None:
    """Build a model from AGENT_MODEL_* variables; None when not configured.

    All three variables are required together: a half-configured model is
    treated as unconfigured rather than silently misbehaving.
    """
    import os

    source = os.environ if environ is None else environ
    base_url = source.get("AGENT_MODEL_BASE_URL", "").strip()
    api_key = source.get("AGENT_MODEL_API_KEY", "").strip()
    model = source.get("AGENT_MODEL_NAME", "").strip()
    if not (base_url and api_key and model):
        return None
    return OpenAICompatibleModel(base_url, api_key, model)


__all__ = ["ModelRequestError", "OpenAICompatibleModel", "model_from_environment"]

"""Stdlib HTTP API exposing the graph runtime and the composable agent shell.

The server is deliberately dependency-free: ``ThreadingHTTPServer`` handles one
request per thread, runs stay synchronous (the POST response contains the final
``RunResult``), and persistence reuses :class:`~agent_core.persistence.RunStore`.
It also serves the static ``frontend/`` workbench from the same origin.

Start locally::

    python -m agent_core.server --port 8000 --frontend-dir frontend
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .agent import Agent
from .nodes import build_agent_from_config
from .persistence import RunStore, _lifecycle_event_to_dict
from .runtime import Runtime, _validate_run_id

logger = logging.getLogger("agent_core.server")

_RUNS_PATH = re.compile(r"^/api/runs/([A-Za-z0-9][A-Za-z0-9_-]{0,127})(/.*)?$")
_MAX_BODY_BYTES = 1_000_000
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class ApiError(Exception):
    """Request-scoped error mapped to an HTTP status and JSON error payload."""

    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class AgentRegistry:
    """Process-local mapping of agent names to configured agents."""

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._lock = threading.RLock()

    def register(self, agent: Agent) -> None:
        if not isinstance(agent, Agent):
            raise TypeError("agent must be an Agent instance")
        with self._lock:
            if agent.spec.name in self._agents:
                raise ValueError(f"agent already registered: {agent.spec.name}")
            self._agents[agent.spec.name] = agent

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


def _build_workbench(runtime: Runtime, registry: Any) -> Agent:
    """Deterministic demo agent backing the bundled frontend workbench.

    It decomposes the requested task text into plan entries, runs a registered
    text-stats tool over the task, and produces a summary. No model or network
    access is involved; the point is to exercise the real runtime boundaries
    (graph execution, tool gating, trace and lifecycle persistence).
    """
    from .agent import AgentBuilder
    from .results import NodeResult

    def intake(ctx):
        task = ctx.input.get("task")
        if not isinstance(task, str) or not task.strip():
            return NodeResult.blocked("EMPTY_TASK")
        return NodeResult.success({"task": task.strip()}, route="planned")

    def plan(ctx):
        task = ctx.state["task"]
        pieces = [piece.strip() for piece in re.split(r"[。;；!！?\n]+", task) if piece.strip()]
        entries = pieces or [task]
        plan_items = [{"index": index + 1, "title": entry[:60]} for index, entry in enumerate(entries)]
        return NodeResult.success(
            {"plan": plan_items, "step_count": len(plan_items)}, route="executed"
        )

    def execute(ctx):
        stats = ctx.tools.invoke("text_stats", {"task": ctx.state["task"]})
        if stats.status.value != "COMPLETED":
            return NodeResult.failed("TOOL_EXECUTION_FAILED")
        processed = [item["title"] for item in ctx.state["plan"]]
        return NodeResult.success(
            {"text_stats": dict(stats.output), "processed": processed}, route="summarized"
        )

    def summarize(ctx):
        task = ctx.state["task"]
        count = ctx.state["step_count"]
        summary = f"已完成对“{task}”的 {count} 个步骤的确定性处理。"
        return NodeResult.success({"summary": summary})

    return (
        AgentBuilder(
            "workbench",
            instructions="接收任务文本,拆解为计划步骤,执行统计并输出摘要。",
            description="Deterministic workbench demo agent.",
            tools=registry,
        )
        .add_node("intake", intake)
        .add_node("plan", plan)
        .add_node("execute", execute)
        .add_node("summarize", summarize)
        .set_entry("intake")
        .add_edge("intake", "plan", when="planned")
        .add_edge("plan", "execute", when="executed")
        .add_edge("execute", "summarize", when="summarized")
        .build(runtime=runtime)
    )


def build_default_registry(*, runtime: Runtime) -> AgentRegistry:
    """Registry with the bundled demo agent; extend it via ``register``."""
    registry = AgentRegistry()
    registry.register(_build_workbench(runtime, _default_tools()))
    return registry


def load_agents_from_config(
    path: str | Path,
    *,
    runtime: Runtime,
    tools: Any = None,
) -> list[Agent]:
    """Build agents from a JSON config file (single config or ``agents`` list)."""
    from .tools import Tool, ToolRegistry

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, Mapping) and isinstance(payload.get("agents"), list):
        configs: list[Mapping[str, Any]] = payload["agents"]
    elif isinstance(payload, Mapping):
        configs = [payload]
    else:
        raise ValueError("agent config file must be an object or {'agents': [...]}")
    if tools is None:
        tools = _default_tools()
    agents = [build_agent_from_config(config, runtime=runtime, tools=tools) for config in configs]
    for agent in agents:
        if not agent.spec.description:
            logger.warning("config agent '%s' has no description", agent.spec.name)
    return agents


def _result_payload(result: Any, *, resumable: bool = False) -> dict[str, Any]:
    """Serialize a RunResult with the same shapes the RunStore persists."""
    return {
        "run_id": result.run_id,
        "status": result.status.value,
        "reason_code": result.reason_code,
        "stopped_at": result.stopped_at,
        "simulated": result.simulated,
        "outputs": result.outputs,
        "state": result.state,
        "trace": [asdict(event) for event in result.trace],
        "lifecycle_events": [
            _lifecycle_event_to_dict(event) for event in result.lifecycle_events
        ],
        "resumable": resumable,
    }


class RuntimeAPIHandler(BaseHTTPRequestHandler):
    server_version = "AgentCoreServer/0.1"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------
    @property
    def registry(self) -> AgentRegistry:
        return self.server.registry  # type: ignore[attr-defined]

    @property
    def runtime(self) -> Runtime:
        return self.server.runtime  # type: ignore[attr-defined]

    @property
    def store(self) -> RunStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def frontend_dir(self) -> Path | None:
        return getattr(self.server, "frontend_dir", None)  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, status: HTTPStatus, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, error: ApiError) -> None:
        self._send_json(error.status, {"error": {"code": error.code, "message": error.message}})

    def _read_json_object(self, *, allow_empty: bool = False) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or "0")
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_CONTENT_LENGTH", "Content-Length is invalid")
        if length < 0 or length > _MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "BODY_TOO_LARGE", "request body is too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            if allow_empty:
                return {}
            raise ApiError(HTTPStatus.BAD_REQUEST, "EMPTY_BODY", "request body must be a JSON object")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_JSON", "request body is not valid JSON")
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_JSON", "request body must be a JSON object")
        return payload

    # -- routing ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            if path == "/api/health":
                self._handle_health()
            elif path == "/api/agents":
                self._handle_agents()
            elif path == "/api/runs":
                self._handle_list_runs()
            elif (match := _RUNS_PATH.match(path)) is not None:
                run_id, suffix = match.group(1), match.group(2) or ""
                if suffix == "":
                    self._handle_get_run(run_id)
                elif suffix == "/status":
                    self._handle_run_status(run_id)
                elif suffix == "/events":
                    self._handle_run_events(run_id)
                elif suffix == "/events/stream":
                    self._handle_run_events_stream(run_id)
                else:
                    raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"unknown path: {path}")
            else:
                self._serve_static(path)
        except ApiError as error:
            self._send_error_json(error)
        except Exception:
            logger.exception("unhandled server error for %s", path)
            self._send_error_json(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "internal server error"))

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            if path == "/api/runs":
                self._handle_create_run()
            elif (match := _RUNS_PATH.match(path)) is not None and match.group(2) == "/cancel":
                self._handle_cancel(match.group(1))
            elif (match := _RUNS_PATH.match(path)) is not None and match.group(2) == "/resume":
                self._handle_resume(match.group(1))
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"unknown path: {path}")
        except ApiError as error:
            self._send_error_json(error)
        except Exception:
            logger.exception("unhandled server error for %s", path)
            self._send_error_json(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "internal server error"))

    # -- API handlers ----------------------------------------------------
    def _handle_health(self) -> None:
        self._send_json(HTTPStatus.OK, {
            "status": "ok",
            "runtime": "agent-core/0.1",
            "agents": self.registry.names(),
        })

    def _handle_agents(self) -> None:
        self._send_json(HTTPStatus.OK, {"agents": self.registry.descriptions()})

    def _handle_create_run(self) -> None:
        payload = self._read_json_object()
        agent_name = payload.get("agent")
        if not isinstance(agent_name, str) or not agent_name.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "MISSING_AGENT", "'agent' must be a nonempty string")
        input_data = payload.get("input")
        if not isinstance(input_data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_INPUT", "'input' must be a JSON object")
        wait = payload.get("wait", True)
        if not isinstance(wait, bool):
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST", "'wait' must be a boolean")
        run_id = payload.get("run_id")
        if run_id is not None:
            if not isinstance(run_id, str):
                raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_RUN_ID", "'run_id' must be a string")
            try:
                _validate_run_id(run_id)
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_RUN_ID", str(exc))
        agent = self.registry.get(agent_name)
        if agent is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "UNKNOWN_AGENT", f"unknown agent: {agent_name}")
        if wait:
            from uuid import uuid4

            if run_id is None:
                run_id = uuid4().hex
            self.server.record_run_agent(run_id, agent_name)  # type: ignore[attr-defined]
            try:
                result = agent.run(input_data, run_id=run_id).run
            except TypeError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_INPUT", str(exc))
            except ValueError as exc:
                raise self._run_conflict_error(exc)
            self._send_json(HTTPStatus.OK, {
                "run": _result_payload(result, resumable=self._is_resumable(result)),
            })
            return
        if run_id is None:
            from uuid import uuid4
            run_id = uuid4().hex
        self.server.record_run_agent(run_id, agent_name)  # type: ignore[attr-defined]
        if not self.server.track_active(run_id):  # type: ignore[attr-defined]
            raise ApiError(HTTPStatus.CONFLICT, "RUN_ALREADY_ACTIVE", f"run_id is already executing: {run_id}")
        try:
            known_status = self.runtime.get_status(run_id)
        except ValueError as exc:
            self.server.release_active(run_id)  # type: ignore[attr-defined]
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_RUN_ID", str(exc))
        if known_status is not None:
            self.server.release_active(run_id)  # type: ignore[attr-defined]
            raise ApiError(HTTPStatus.CONFLICT, "RUN_ID_ALREADY_USED", f"run_id already has a lifecycle: {run_id}")
        worker = threading.Thread(
            target=self._execute_async_run, args=(agent, input_data, run_id), daemon=True,
        )
        worker.start()
        self._send_json(HTTPStatus.ACCEPTED, {"run_id": run_id, "status": "PENDING"})

    @staticmethod
    def _run_conflict_error(exc: ValueError) -> ApiError:
        message = str(exc)
        if "already executing" in message:
            return ApiError(HTTPStatus.CONFLICT, "RUN_ALREADY_ACTIVE", message)
        if "already has a" in message:
            return ApiError(HTTPStatus.CONFLICT, "RUN_ID_ALREADY_USED", message)
        if "cannot resume" in message:
            return ApiError(HTTPStatus.CONFLICT, "NOT_RESUMABLE", message)
        if "does not match" in message or "does not exist" in message or "no next_node" in message:
            return ApiError(HTTPStatus.CONFLICT, "CHECKPOINT_INCOMPATIBLE", message)
        return ApiError(HTTPStatus.BAD_REQUEST, "INVALID_RUN", message)

    def _execute_async_run(self, agent: Agent, input_data: dict[str, Any], run_id: str) -> None:
        try:
            agent.run(input_data, run_id=run_id)
        except Exception:
            logger.exception("async run %s failed", run_id)
        finally:
            self.server.release_active(run_id)  # type: ignore[attr-defined]

    def _handle_list_runs(self) -> None:
        self._send_json(HTTPStatus.OK, {
            "runs": self.store.list_runs(),
            "active": self.server.active_run_ids(),  # type: ignore[attr-defined]
        })

    def _is_resumable(self, result: Any) -> bool:
        """A run is resumable when it failed or blocked past its first boundary."""
        if result.status.value not in ("BLOCKED", "FAILED"):
            return False
        if self.server.checkpoint_store is None:  # type: ignore[attr-defined]
            return False
        try:
            checkpoint = self.server.checkpoint_store.load(result.run_id)  # type: ignore[attr-defined]
        except ValueError:
            return False
        return checkpoint is not None and checkpoint.next_node is not None

    def _handle_get_run(self, run_id: str) -> None:
        import time as _time

        result = None
        for attempt in range(20):
            try:
                result = self.store.load(run_id)
            except ValueError as exc:
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "INVALID_RUN_RECORD", str(exc))
            if result is not None:
                break
            # 运行时在内存中先于落盘到达终态;对已知终态运行的读取做有界等待,
            # 保证 "status 终态 ⇒ 结果可取" 的接口自洽。
            try:
                status = self.runtime.get_status(run_id)
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_RUN_ID", str(exc))
            if status is None or not status.terminal:
                break
            _time.sleep(0.05)
        if result is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "UNKNOWN_RUN", f"unknown run: {run_id}")
        self._send_json(HTTPStatus.OK, {
            "run": _result_payload(result, resumable=self._is_resumable(result)),
        })

    def _handle_resume(self, run_id: str) -> None:
        from dataclasses import replace

        payload = self._read_json_object(allow_empty=True)
        state_updates = payload.get("state_updates", {})
        if not isinstance(state_updates, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_STATE_UPDATES", "'state_updates' must be an object")
        try:
            status = self.runtime.get_status(run_id)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_RUN_ID", str(exc))
        if status is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "UNKNOWN_RUN", f"unknown run: {run_id}")
        agent_name = self.server.agent_for_run(run_id)  # type: ignore[attr-defined]
        if agent_name is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "RUN_AGENT_UNKNOWN", f"no registered agent for run: {run_id}")
        agent = self.registry.get(agent_name)
        if agent is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "UNKNOWN_AGENT", f"unknown agent: {agent_name}")
        if status.value in ("RUNNING", "PENDING"):
            raise ApiError(HTTPStatus.CONFLICT, "RUN_ALREADY_ACTIVE", f"run is already executing: {run_id}")
        if status.value not in ("BLOCKED", "FAILED"):
            raise ApiError(HTTPStatus.CONFLICT, "NOT_RESUMABLE", f"run is not resumable from status: {status.value}")
        if self.server.checkpoint_store is None:  # type: ignore[attr-defined]
            raise ApiError(HTTPStatus.CONFLICT, "NOT_RESUMABLE", "server has no checkpoint store")
        try:
            checkpoint = self.server.checkpoint_store.load(run_id)
        except ValueError as exc:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "INVALID_CHECKPOINT", str(exc))
        if checkpoint is None or checkpoint.next_node is None:
            raise ApiError(HTTPStatus.CONFLICT, "NOT_RESUMABLE", "run has no resumable checkpoint boundary")
        if state_updates:
            merged = dict(checkpoint.state)
            merged.update(state_updates)
            from .runtime import _json_snapshot as _snapshot

            try:
                merged = _snapshot(merged)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_STATE_UPDATES", f"state updates must be JSON-safe: {exc}")
            checkpoint = replace(
                checkpoint, state=merged, state_version=checkpoint.state_version + 1,
            )
        try:
            result = self.runtime.resume(agent.graph, checkpoint)
        except ValueError as exc:
            raise self._run_conflict_error(exc)
        self._send_json(HTTPStatus.OK, {
            "run": _result_payload(result, resumable=self._is_resumable(result)),
            "state_updates_applied": bool(state_updates),
        })

    def _handle_run_status(self, run_id: str) -> None:
        try:
            _validate_run_id(run_id)
            status = self.runtime.get_status(run_id)
            if status is None and run_id in self.server.active_run_ids():  # type: ignore[attr-defined]
                self._send_json(HTTPStatus.OK, {"run_id": run_id, "status": "PENDING"})
                return
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_RUN_ID", str(exc))
        if status is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "UNKNOWN_RUN", f"unknown run: {run_id}")
        self._send_json(HTTPStatus.OK, {"run_id": run_id, "status": status.value})

    def _handle_run_events(self, run_id: str) -> None:
        try:
            events = self.runtime.get_lifecycle_events(run_id)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_RUN_ID", str(exc))
        if not events:
            raise ApiError(HTTPStatus.NOT_FOUND, "UNKNOWN_RUN", f"unknown run: {run_id}")
        self._send_json(HTTPStatus.OK, {
            "run_id": run_id,
            "events": [_lifecycle_event_to_dict(event) for event in events],
        })

    def _handle_cancel(self, run_id: str) -> None:
        try:
            accepted = self.runtime.cancel(run_id)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_RUN_ID", str(exc))
        self._send_json(HTTPStatus.OK, {"run_id": run_id, "accepted": accepted})

    _SSE_POLL_SECONDS = 0.15
    _SSE_HEARTBEAT_SECONDS = 15.0
    _SSE_MAX_SECONDS = 900.0

    def _handle_run_events_stream(self, run_id: str) -> None:
        """Push lifecycle events over Server-Sent Events until a terminal status.

        The stream starts with events already persisted (replaying them), then
        follows the run in-process. It ends with an ``event: done`` frame that
        carries the terminal status; clients keep the regular result endpoints
        for the full RunResult.
        """
        import time as _time

        try:
            _validate_run_id(run_id)
            status = self.runtime.get_status(run_id)
            if status is None and run_id not in self.server.active_run_ids():  # type: ignore[attr-defined]
                raise ApiError(HTTPStatus.NOT_FOUND, "UNKNOWN_RUN", f"unknown run: {run_id}")
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_RUN_ID", str(exc))

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        def send(event_name: str, payload: Mapping[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            self.wfile.write(f"event: {event_name}\ndata: {body}\n\n".encode("utf-8"))
            self.wfile.flush()

        sent = 0
        started = _time.monotonic()
        last_heartbeat = started
        try:
            while True:
                events = self.runtime.get_lifecycle_events(run_id)
                for event in events[sent:]:
                    send("lifecycle", _lifecycle_event_to_dict(event))
                    sent += 1
                status = self.runtime.get_status(run_id)
                if status is not None and status.terminal:
                    send("done", {"run_id": run_id, "status": status.value})
                    return
                now = _time.monotonic()
                if now - last_heartbeat >= self._SSE_HEARTBEAT_SECONDS:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = now
                if now - started >= self._SSE_MAX_SECONDS:
                    send("done", {"run_id": run_id, "status": "STREAM_TIMEOUT"})
                    return
                if status is None and run_id not in self.server.active_run_ids():  # type: ignore[attr-defined]
                    send("done", {"run_id": run_id, "status": "UNKNOWN"})
                    return
                _time.sleep(self._SSE_POLL_SECONDS)
        except (ConnectionError, BrokenPipeError, OSError):
            return

    # -- static files ----------------------------------------------------
    def _serve_static(self, path: str) -> None:
        if self.frontend_dir is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"unknown path: {path}")
        if path in ("/", ""):
            path = "/index.html"
        candidate = (self.frontend_dir / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(self.frontend_dir.resolve())
        except ValueError:
            raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "path is outside the frontend directory")
        if not candidate.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"no such file: {path}")
        content_type = _STATIC_TYPES.get(candidate.suffix.lower())
        if content_type is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"unsupported file type: {candidate.suffix}")
        body = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        runtime: Runtime,
        registry: AgentRegistry,
        store: RunStore,
        frontend_dir: Path | None,
        checkpoint_store: Any = None,
    ) -> None:
        super().__init__(address, RuntimeAPIHandler)
        self.runtime = runtime
        self.registry = registry
        self.store = store
        self.frontend_dir = frontend_dir
        self.checkpoint_store = checkpoint_store
        self._active_runs: set[str] = set()
        self._active_lock = threading.Lock()
        self._run_agents: dict[str, str] = {}
        self._run_agents_lock = threading.Lock()

    def track_active(self, run_id: str) -> bool:
        """Claim a run_id for async execution; False when already claimed."""
        with self._active_lock:
            if run_id in self._active_runs:
                return False
            self._active_runs.add(run_id)
            return True

    def release_active(self, run_id: str) -> None:
        with self._active_lock:
            self._active_runs.discard(run_id)

    def active_run_ids(self) -> list[str]:
        with self._active_lock:
            return sorted(self._active_runs)

    def record_run_agent(self, run_id: str, agent_name: str) -> None:
        """Remember which agent executed a run so resume can find its graph."""
        with self._run_agents_lock:
            self._run_agents[run_id] = agent_name

    def agent_for_run(self, run_id: str) -> str | None:
        with self._run_agents_lock:
            return self._run_agents.get(run_id)


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    store_dir: str | Path = "outputs/server_runs",
    checkpoint_dir: str | Path | None = None,
    frontend_dir: str | Path | None = None,
    runtime: Runtime | None = None,
    registry: AgentRegistry | None = None,
) -> AgentServer:
    """Assemble a server with a shared Runtime, RunStore and agent registry."""
    store = RunStore(store_dir)
    if checkpoint_dir is None:
        checkpoint_dir = Path(store_dir).parent / "checkpoints"
    from .checkpoint import FileCheckpointStore

    checkpoint_store = FileCheckpointStore(checkpoint_dir)
    if runtime is None:
        runtime = Runtime(store=store, checkpoint_store=checkpoint_store)
    if registry is None:
        registry = build_default_registry(runtime=runtime)
    return AgentServer(
        (host, port),
        runtime=runtime,
        registry=registry,
        store=store,
        frontend_dir=Path(frontend_dir) if frontend_dir is not None else _default_frontend_dir(),
        checkpoint_store=checkpoint_store,
    )


def _default_tools() -> Any:
    from .tools import Tool, ToolRegistry

    tools = ToolRegistry()
    tools.register(
        Tool("text_stats", lambda args: {"chars": len(args["task"]), "words": len(args["task"].split())})
    )
    return tools


def _default_frontend_dir() -> Path | None:
    local = Path.cwd() / "frontend"
    if local.is_dir():
        return local
    packaged = Path(__file__).resolve().parent.parent / "frontend"
    if packaged.is_dir():
        return packaged
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the agent-core runtime over HTTP.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    parser.add_argument(
        "--store-dir", default="outputs/server_runs",
        help="directory for persisted run records (default: outputs/server_runs)",
    )
    parser.add_argument(
        "--frontend-dir", default=None,
        help="directory with the static frontend (default: ./frontend, then the repo checkout)",
    )
    parser.add_argument(
        "--config", default=None, action="append",
        help="JSON file with declarative agent configs to load at startup (repeatable)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    server = create_server(
        host=args.host, port=args.port, store_dir=args.store_dir, frontend_dir=args.frontend_dir,
    )
    if args.config:
        try:
            for config_path in args.config:
                for agent in load_agents_from_config(config_path, runtime=server.runtime):
                    server.registry.register(agent)
                logger.info("loaded agents from %s", config_path)
        except (OSError, ValueError, TypeError) as exc:
            logger.error("failed to load agent config: %s", exc)
            server.server_close()
            return 2
    bound_host, bound_port = server.server_address[:2]
    logger.info("agent-core server listening on http://%s:%s", bound_host, bound_port)
    logger.info("agents: %s", ", ".join(server.registry.names()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

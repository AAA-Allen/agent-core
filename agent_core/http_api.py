"""HTTP request surface: routing, handlers, SSE and static files."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from . import __version__
from .agent import Agent
from .persistence import RunStore, _lifecycle_event_to_dict
from .registry import AgentRegistry
from .runtime import Runtime, _validate_run_id

logger = logging.getLogger("agent_core.http_api")


_RUNS_PATH = re.compile(r"^/api/runs/([A-Za-z0-9][A-Za-z0-9_-]{0,127})(/.*)?$")
_SESSIONS_PATH = re.compile(r"^/api/sessions/([A-Za-z0-9][A-Za-z0-9_-]{0,127})$")
_RUN_STATUSES = ("COMPLETED", "BLOCKED", "FAILED", "CANCELED")
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


def _result_payload(result: Any, *, resumable: bool = False) -> dict[str, Any]:
    """Serialize a RunResult with the same shapes the RunStore persists."""
    return {
        "run_id": result.run_id,
        "status": result.status.value,
        "reason_code": result.reason_code,
        "stopped_at_node": result.stopped_at_node,
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
    server_version = f"AgentCoreServer/{__version__}"
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
                elif suffix == "/checkpoint":
                    self._handle_get_checkpoint(run_id)
                else:
                    raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"unknown path: {path}")
            elif path == "/api/sessions":
                self._handle_list_sessions()
            elif (match := _SESSIONS_PATH.match(path)) is not None:
                self._handle_get_session(match.group(1))
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
            self._check_request_origin()
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

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            self._check_request_origin()
            if (match := _SESSIONS_PATH.match(path)) is not None:
                self._handle_clear_session(match.group(1))
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "NOT_FOUND", f"unknown path: {path}")
        except ApiError as error:
            self._send_error_json(error)
        except Exception:
            logger.exception("unhandled server error for %s", path)
            self._send_error_json(ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", "internal server error"))

    # -- API handlers ----------------------------------------------------
    def _handle_health(self) -> None:
        model = self.server.model  # type: ignore[attr-defined]
        self._send_json(HTTPStatus.OK, {
            "status": "ok",
            "runtime": f"agent-core/{__version__}",
            "agents": self.registry.names(),
            "model_configured": model is not None,
            "model_name": getattr(model, "model", None),
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
        session_id = payload.get("session_id")
        if session_id is not None:
            if not isinstance(session_id, str) or not session_id.strip():
                raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_SESSION", "'session_id' must be a nonempty string")
            try:
                _validate_run_id(session_id)
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_SESSION", str(exc))
        memory = self.server.sessions.memory_for(session_id) if session_id else None
        agent = self.registry.build(agent_name, memory=memory)
        if agent is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "UNKNOWN_AGENT", f"unknown agent: {agent_name}")
        if wait:
            from uuid import uuid4

            if run_id is None:
                run_id = uuid4().hex
            self.server.record_run_agent(run_id, agent_name, session_id)  # type: ignore[attr-defined]
            try:
                result = agent.run(input_data, run_id=run_id).run
            except TypeError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_INPUT", str(exc))
            except ValueError as exc:
                raise self._run_conflict_error(exc)
            self._send_json(HTTPStatus.OK, {
                "run": _result_payload(result, resumable=self._is_resumable(result)),
                **self._session_block(session_id),
            })
            return
        if run_id is None:
            from uuid import uuid4
            run_id = uuid4().hex
        self.server.record_run_agent(run_id, agent_name, session_id)  # type: ignore[attr-defined]
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
        self._send_json(HTTPStatus.ACCEPTED, {
            "run_id": run_id,
            "status": "PENDING",
            **self._session_block(session_id),
        })

    def _session_block(self, session_id: str | None) -> dict[str, Any]:
        if not session_id:
            return {}
        snapshot = self.server.sessions.snapshot(session_id)  # type: ignore[attr-defined]
        return {"session": {"id": session_id, "memory": snapshot if snapshot is not None else {}}}

    @staticmethod
    def _run_conflict_error(exc: ValueError) -> ApiError:
        reason = getattr(exc, "reason_code", None)
        if isinstance(reason, str) and reason.strip():
            return ApiError(HTTPStatus.CONFLICT, reason, str(exc))
        return ApiError(HTTPStatus.BAD_REQUEST, "INVALID_RUN", str(exc))

    def _check_request_origin(self) -> None:
        """Reject cross-origin and rebound-host state-changing requests.

        Browsers attach an ``Origin`` header to cross-site POSTs, so a
        mismatched origin means another page is trying to drive this local
        server (localhost CSRF); a ``Host`` header naming anything other than
        the bound host or loopback names is a DNS-rebinding probe. Non-browser
        clients (curl, SDKs) send no Origin and pass.
        """
        origin = self.headers.get("Origin")
        if origin is not None:
            parsed = urlsplit(origin)
            origin_host = parsed.hostname or ""
            allowed_hosts = {"127.0.0.1", "localhost", "::1", self.server.server_address[0]}
            if origin_host not in allowed_hosts:
                raise ApiError(HTTPStatus.FORBIDDEN, "FORBIDDEN_ORIGIN", f"origin not allowed: {origin}")
        host_header = self.headers.get("Host", "")
        host_name = host_header.partition(":")[0].strip("[]").lower()
        allowed_hosts = {"127.0.0.1", "localhost", "::1", self.server.server_address[0].lower()}
        if host_name and host_name not in allowed_hosts:
            raise ApiError(HTTPStatus.FORBIDDEN, "FORBIDDEN_HOST", f"host not allowed: {host_header}")

    def _execute_async_run(self, agent: Agent, input_data: dict[str, Any], run_id: str) -> None:
        try:
            agent.run(input_data, run_id=run_id)
        except Exception:
            logger.exception("async run %s failed", run_id)
        finally:
            self.server.release_active(run_id)  # type: ignore[attr-defined]

    def _handle_list_runs(self) -> None:
        from urllib.parse import parse_qs

        query = parse_qs(urlsplit(self.path).query)

        def int_param(name: str, default: int, low: int, high: int) -> int:
            values = query.get(name)
            if not values:
                return default
            try:
                value = int(values[0])
            except ValueError:
                raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_QUERY", f"'{name}' must be an integer")
            if value < low or value > high:
                raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_QUERY", f"'{name}' must be between {low} and {high}")
            return value

        limit = int_param("limit", 20, 1, 100)
        offset = int_param("offset", 0, 0, 1_000_000)
        status_values = query.get("status")
        status_filter = status_values[0] if status_values else None
        if status_filter is not None and status_filter not in _RUN_STATUSES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_QUERY", f"'status' must be one of {', '.join(_RUN_STATUSES)}")

        entries = []
        for run_id in self.store.list_runs():
            entry = self._run_summary(run_id)
            if entry is None:
                continue
            if status_filter is not None and entry["status"] != status_filter:
                continue
            entries.append(entry)
        entries.sort(key=lambda entry: (entry["started_at"] or "", entry["run_id"]), reverse=True)
        self._send_json(HTTPStatus.OK, {
            "runs": entries[offset:offset + limit],
            "total": len(entries),
            "offset": offset,
            "limit": limit,
            "active": self.server.active_run_ids(),  # type: ignore[attr-defined]
        })

    def _run_summary(self, run_id: str) -> dict[str, Any] | None:
        """Build a run summary, reusing cached metadata while the file is unchanged.

        Records are keyed by their file mtime: resume overwrites the record and
        invalidates the cached entry, so status changes are always visible.
        The agent attribution is injected per read (it can change independently).
        """
        path = self.store.directory / f"{run_id}.json"
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = None
        with self.server._summary_lock:  # type: ignore[attr-defined]
            cached = self.server._summary_cache.get(run_id)  # type: ignore[attr-defined]
        if cached is not None and cached[0] == mtime:
            entry = dict(cached[1])
        else:
            entry = self._load_run_summary(run_id)
            with self.server._summary_lock:  # type: ignore[attr-defined]
                self.server._summary_cache[run_id] = (mtime, dict(entry))  # type: ignore[attr-defined]
        entry["agent"] = self.server.agent_for_run(run_id)  # type: ignore[attr-defined]
        return entry

    def _load_run_summary(self, run_id: str) -> dict[str, Any]:
        try:
            result = self.store.load(run_id)
        except ValueError:
            return {
                "run_id": run_id,
                "status": "UNREADABLE",
                "reason_code": None,
                "started_at": None,
                "stopped_at_node": None,
                "summary": None,
                "simulated": False,
            }
        state = result.state or {}
        return {
            "run_id": result.run_id,
            "status": result.status.value,
            "reason_code": result.reason_code,
            "started_at": result.trace[0].started_at if result.trace else None,
            "stopped_at_node": result.stopped_at_node,
            "summary": state.get("summary") if isinstance(state, dict) else None,
            "simulated": result.simulated,
        }

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
        session_id = self.server.session_for_run(run_id)  # type: ignore[attr-defined]
        self._send_json(HTTPStatus.OK, {
            "run": _result_payload(result, resumable=self._is_resumable(result)),
            **self._session_block(session_id),
        })

    def _handle_get_checkpoint(self, run_id: str) -> None:
        if self.server.checkpoint_store is None:  # type: ignore[attr-defined]
            raise ApiError(HTTPStatus.CONFLICT, "NOT_RESUMABLE", "server has no checkpoint store")
        try:
            checkpoint = self.server.checkpoint_store.load(run_id)
        except ValueError as exc:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "INVALID_CHECKPOINT", str(exc))
        if checkpoint is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "UNKNOWN_CHECKPOINT", f"no checkpoint for run: {run_id}")
        self._send_json(HTTPStatus.OK, {
            "run_id": run_id,
            "checkpoint": {
                "next_node": checkpoint.next_node,
                "state": checkpoint.state,
                "state_version": checkpoint.state_version,
                "step_count": checkpoint.step_count,
            },
            "status": (self.runtime.get_status(run_id).value if self.runtime.get_status(run_id) else None),
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

    def _handle_list_sessions(self) -> None:
        self._send_json(HTTPStatus.OK, {"sessions": self.server.sessions.ids()})  # type: ignore[attr-defined]

    def _handle_get_session(self, session_id: str) -> None:
        try:
            _validate_run_id(session_id)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_SESSION", str(exc))
        snapshot = self.server.sessions.snapshot(session_id)  # type: ignore[attr-defined]
        if snapshot is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "UNKNOWN_SESSION", f"unknown session: {session_id}")
        self._send_json(HTTPStatus.OK, {"session_id": session_id, "memory": snapshot})

    def _handle_clear_session(self, session_id: str) -> None:
        try:
            _validate_run_id(session_id)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "INVALID_SESSION", str(exc))
        cleared = self.server.sessions.clear(session_id)  # type: ignore[attr-defined]
        if not cleared:
            raise ApiError(HTTPStatus.NOT_FOUND, "UNKNOWN_SESSION", f"unknown session: {session_id}")
        self._send_json(HTTPStatus.OK, {"session_id": session_id, "cleared": True})

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

        from .streaming import default_bus

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

        bus = default_bus()
        sent = 0
        token_index = 0
        started = _time.monotonic()
        last_heartbeat = started
        try:
            while True:
                events = self.runtime.get_lifecycle_events(run_id)
                for event in events[sent:]:
                    send("lifecycle", _lifecycle_event_to_dict(event))
                    sent += 1
                deltas, _truncated, _closed = bus.history(run_id, after=token_index)
                for node_id, text in deltas:
                    send("token", {"run_id": run_id, "node_id": node_id, "text": text})
                    token_index += 1
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


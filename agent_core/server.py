"""HTTP server composition: AgentServer, create_server and CLI entry.

The request surface lives in :mod:`agent_core.http_api`, agent registration
and config loading in :mod:`agent_core.registry`, and the bundled demo agent
in :mod:`agent_core.workbench`. This module only composes them and owns the
process lifecycle (active runs, run-index persistence, config hot reload).
Public names are re-exported for compatibility.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .http_api import ApiError, RuntimeAPIHandler
from .persistence import RunStore
from .registry import (
    AgentRegistry,
    SessionManager,
    agent_resolver,
    load_agents_from_config,
    register_agents_from_config,
)
from .runtime import Runtime, _validate_run_id
from .workbench import build_default_registry

__all__ = [
    "AgentRegistry", "SessionManager", "ApiError", "RuntimeAPIHandler",
    "load_agents_from_config", "register_agents_from_config",
    "AgentServer", "create_server", "main",
]

logger = logging.getLogger("agent_core.server")


class StoreInstanceLock:
    """Exclusive OS-level lock on a store directory; auto-released on death.

    Uses ``fcntl.flock`` (POSIX) or ``msvcrt.locking`` (Windows) so the lock
    disappears when the owning process exits — including a hard kill — which
    a lockfile with O_EXCL cannot guarantee. Two server instances sharing
    one store would race the run-index read-modify-write, so a second
    instance is refused with an explicit error.
    """

    def __init__(self, store_dir: str | Path) -> None:
        self.path = Path(store_dir) / ".server.lock"
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(
                f"another agent-core server instance is using {self.path.parent}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None


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
        model: Any = None,
    ) -> None:
        super().__init__(address, RuntimeAPIHandler)
        self.runtime = runtime
        self.registry = registry
        self.store = store
        self.frontend_dir = frontend_dir
        self.checkpoint_store = checkpoint_store
        self.model = model
        self.sessions = SessionManager()
        self.run_index_path = store.directory / ".agent_index.json"
        self._instance_lock = StoreInstanceLock(store.directory)
        try:
            self._instance_lock.acquire()
        except Exception:
            # The listening socket is created by TCPServer before the store
            # lock is acquired. Close it when construction is rejected.
            super().server_close()
            raise
        self._active_runs: set[str] = set()
        self._active_lock = threading.Lock()
        self._run_agents: dict[str, str] = {}
        self._run_sessions: dict[str, str] = {}
        self._run_agents_lock = threading.Lock()
        self._config_names: dict[str, list[str]] = {}
        self._config_lock = threading.RLock()
        self._summary_cache: dict[str, tuple[int | None, dict[str, Any]]] = {}
        self._summary_lock = threading.Lock()
        self._load_run_index()

    def server_close(self) -> None:
        super().server_close()
        self._instance_lock.release()

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

    def record_run_agent(self, run_id: str, agent_name: str, session_id: str | None = None) -> None:
        """Remember which agent (and session) executed a run; index survives restarts."""
        with self._run_agents_lock:
            self._run_agents[run_id] = agent_name
            if session_id:
                self._run_sessions[run_id] = session_id
        self._persist_run_index()

    def agent_for_run(self, run_id: str) -> str | None:
        with self._run_agents_lock:
            return self._run_agents.get(run_id)

    def session_for_run(self, run_id: str) -> str | None:
        with self._run_agents_lock:
            return self._run_sessions.get(run_id)

    def note_config_ownership(self, path: str | Path, names: list[str]) -> None:
        """Record which agent names a config file owns (for hot reload)."""
        with self._config_lock:
            self._config_names[str(path)] = list(names)

    def reload_config(self, path: str | Path) -> list[str]:
        """Rebuild a config file's agents in place; keeps old agents on failure.

        The new config is fully validated (and built) before any registration
        changes, so a broken edit never leaves the registry half-updated.
        """
        key = str(path)
        agents = load_agents_from_config(
            path, runtime=self.runtime, agent_resolver=agent_resolver(self.registry),
            model=self.model,
        )
        new_names = [agent.spec.name for agent in agents]
        with self._config_lock:
            owned = list(self._config_names.get(key, []))
        foreign = [
            name for name in new_names
            if self.registry.get(name) is not None and name not in owned
        ]
        if foreign:
            raise ValueError(f"cannot reload config owning other agents' names: {', '.join(foreign)}")
        for name in owned:
            self.registry.unregister(name)
        registered = register_agents_from_config(
            path, self.registry, runtime=self.runtime, model=self.model,
        )
        with self._config_lock:
            self._config_names[key] = registered
        return registered

    def _persist_run_index(self) -> None:
        import json as _json
        import uuid as _uuid

        with self._run_agents_lock:
            payload = {
                "schema_version": 1,
                "run_agents": dict(self._run_agents),
            }
        target = self.run_index_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.parent / f".{target.name}.{_uuid.uuid4().hex}.tmp"
            tmp.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(target)
        except OSError:
            logger.warning("could not persist run agent index at %s", target)

    def _load_run_index(self) -> None:
        import json as _json

        target = self.run_index_path
        if not target.is_file():
            return
        try:
            payload = _json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version", 1) != 1:
                raise ValueError("unsupported run index schema_version")
            run_agents = payload.get("run_agents", {})
            if not isinstance(run_agents, dict):
                raise ValueError("run index run_agents must be an object")
            with self._run_agents_lock:
                for run_id, agent_name in run_agents.items():
                    _validate_run_id(run_id)
                    if isinstance(agent_name, str) and agent_name.strip():
                        self._run_agents[run_id] = agent_name
        except (OSError, ValueError) as exc:
            logger.warning("ignoring unreadable run agent index %s: %s", target, exc)


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    store_dir: str | Path = "outputs/server_runs",
    checkpoint_dir: str | Path | None = None,
    frontend_dir: str | Path | None = None,
    runtime: Runtime | None = None,
    registry: AgentRegistry | None = None,
    model: Any = None,
) -> AgentServer:
    """Assemble a server with a shared Runtime, RunStore and agent registry.

    ``model`` defaults to the ``AGENT_MODEL_*`` environment configuration; a
    half-configured environment is treated as unconfigured.
    """
    from .models import model_from_environment

    if model is None:
        model = model_from_environment()
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
        model=model,
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


def _watch_config_files(server: AgentServer, paths: list[Path], interval: float = 2.0) -> None:
    """Poll config file mtimes and hot-reload changed agent definitions."""
    import time as _time

    def mtime(path: Path) -> float | None:
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    mtimes = {str(path): mtime(path) for path in paths}
    while True:
        _time.sleep(interval)
        for path in paths:
            key = str(path)
            current = mtime(path)
            if current is None or current == mtimes.get(key):
                continue
            mtimes[key] = current
            try:
                names = server.reload_config(path)
                logger.info("hot-reloaded agents from %s: %s", path, ", ".join(names))
            except Exception as exc:
                logger.error("config hot-reload failed for %s (keeping previous agents): %s", path, exc)


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
                registered = register_agents_from_config(config_path, server.registry, runtime=server.runtime)
                server.note_config_ownership(config_path, registered)
                logger.info("loaded agents from %s: %s", config_path, ", ".join(registered))
        except (OSError, ValueError, TypeError) as exc:
            logger.error("failed to load agent config: %s", exc)
            server.server_close()
            return 2
        watcher = threading.Thread(
            target=_watch_config_files, args=(server, [Path(p) for p in args.config]), daemon=True,
        )
        watcher.start()
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

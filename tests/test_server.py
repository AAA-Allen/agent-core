"""HTTP API server contract tests."""

import json
import threading
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path

from agent_core import AgentBuilder, NodeResult, Runtime
from agent_core.server import AgentRegistry, create_server, register_agents_from_config


class ServerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.server = create_server(
            port=0, store_dir=Path(self._tmp.name) / "runs", frontend_dir="frontend",
        )
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        self.addCleanup(self._stop_server)

    def register_recovery_agent(self) -> None:
        from agent_core.server import register_agents_from_config

        register_agents_from_config(
            "examples/recovery_agent.json", self.server.registry, runtime=self.server.runtime,
        )

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5.0)

    def request(self, method: str, path: str, body: str | bytes | None = None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            content_type = response.getheader("Content-Type", "")
            payload = json.loads(raw) if content_type.startswith("application/json") and raw else raw
            return response.status, payload
        finally:
            connection.close()

    def post_json(self, path: str, payload) -> tuple[int, object]:
        return self.request("POST", path, json.dumps(payload))


class ServerApiTests(ServerTestBase):
    def test_health_and_agents(self) -> None:
        status, payload = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["agents"], ["workbench"])
        status, payload = self.request("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual(payload["agents"][0]["name"], "workbench")

    def test_create_run_completes_and_persists(self) -> None:
        status, payload = self.post_json(
            "/api/runs",
            {"agent": "workbench", "input": {"task": "整理本周的项目进展;准备周一评审"}},
        )
        self.assertEqual(status, 200)
        run = payload["run"]
        self.assertEqual(run["status"], "COMPLETED")
        self.assertEqual(run["reason_code"], None)
        self.assertIn("summary", run["state"])
        event_types = [event["event_type"] for event in run["lifecycle_events"]]
        self.assertEqual(event_types, ["run.created", "run.running", "run.completed"])
        node_events = {event["node_id"] for event in run["trace"] if event["node_id"]}
        self.assertEqual(node_events, {"intake", "plan", "execute", "summarize"})

        run_id = run["run_id"]
        status, payload = self.request("GET", "/api/runs")
        self.assertEqual(status, 200)
        self.assertTrue(any(item["run_id"] == run_id for item in payload["runs"]))
        status, payload = self.request("GET", f"/api/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["run"]["state"], run["state"])
        status, payload = self.request("GET", f"/api/runs/{run_id}/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "COMPLETED")
        status, payload = self.request("GET", f"/api/runs/{run_id}/events")
        self.assertEqual(status, 200)
        self.assertEqual(payload["events"][-1]["event_type"], "run.completed")

    def test_create_run_blocked_reports_reason(self) -> None:
        status, payload = self.post_json("/api/runs", {"agent": "workbench", "input": {}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["run"]["status"], "BLOCKED")
        self.assertEqual(payload["run"]["reason_code"], "EMPTY_TASK")
        self.assertEqual(payload["run"]["lifecycle_events"][-1]["event_type"], "run.blocked")

    def test_create_run_validates_request(self) -> None:
        cases = [
            ({"input": {}}, 400, "MISSING_AGENT"),
            ({"agent": "workbench"}, 400, "INVALID_INPUT"),
            ({"agent": "workbench", "input": []}, 400, "INVALID_INPUT"),
            ({"agent": "nope", "input": {}}, 404, "UNKNOWN_AGENT"),
            ({"agent": "workbench", "input": {}, "run_id": "bad id!"}, 400, "INVALID_RUN_ID"),
        ]
        for payload, expected_status, expected_code in cases:
            status, body = self.post_json("/api/runs", payload)
            self.assertEqual(status, expected_status, payload)
            self.assertEqual(body["error"]["code"], expected_code)
        status, body = self.request("POST", "/api/runs", "{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_JSON")
        status, body = self.request("POST", "/api/runs", "")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "EMPTY_BODY")

    def test_unknown_paths_return_not_found(self) -> None:
        status, body = self.request("GET", "/api/nothing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")
        status, _ = self.request("POST", "/api/nothing", "{}")
        self.assertEqual(status, 404)
        status, _ = self.request("GET", "/api/runs/some-id/unknown-suffix")
        self.assertEqual(status, 404)

    def test_unknown_run_queries_return_not_found(self) -> None:
        for path in ("/api/runs/missing-run", "/api/runs/missing-run/status", "/api/runs/missing-run/events"):
            status, body = self.request("GET", path)
            self.assertEqual(status, 404, path)
            self.assertEqual(body["error"]["code"], "UNKNOWN_RUN")
        status, _ = self.request("POST", "/api/runs/missing-run/cancel")
        self.assertEqual(status, 200)

    def test_static_files_served_and_traversal_blocked(self) -> None:
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body)
        status, _ = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/styles.css")
        self.assertEqual(status, 200)
        status, body = self.request("GET", "/../pyproject.toml")
        self.assertEqual(status, 404)
        status, body = self.request("GET", "/missing.css")
        self.assertEqual(status, 404)


    def test_agents_include_graph_topology(self) -> None:
        status, payload = self.request("GET", "/api/agents")
        self.assertEqual(status, 200)
        graph = payload["agents"][0]["graph"]
        self.assertEqual(graph["entry"], "intake")
        self.assertEqual(graph["nodes"], ["intake", "plan", "execute", "summarize"])
        self.assertEqual(len(graph["edges"]), 3)
        self.assertEqual(graph["edges"][0], {"source": "intake", "target": "plan", "when": "planned"})

    def test_config_agents_are_loaded_and_runnable(self) -> None:
        from agent_core.server import load_agents_from_config

        agents = load_agents_from_config("examples/support_agent.json", runtime=self.server.runtime)
        for agent in agents:
            self.server.registry.register(agent)

        status, payload = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["agents"], ["support-triage", "workbench"])

        status, payload = self.post_json(
            "/api/runs", {"agent": "support-triage", "input": {"task": "无法登录;收不到验证码"}},
        )
        self.assertEqual(status, 200)
        run = payload["run"]
        self.assertEqual(run["status"], "COMPLETED")
        self.assertEqual(run["state"]["step_count"], 2)
        self.assertIn("已拆解为 2 个步骤", run["state"]["summary"])

    def test_config_file_rejects_invalid_payload(self) -> None:
        import tempfile
        from agent_core.server import load_agents_from_config

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({"nodes": "not-a-list"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_agents_from_config(bad, runtime=self.server.runtime)


class ServerLifecycleTests(ServerTestBase):
    def _register_slow_agent(self) -> threading.Event:
        release = threading.Event()

        def slow_node(ctx):
            release.wait(timeout=5.0)
            return NodeResult.success({"done": True})

        agent = (
            AgentBuilder("slow-agent")
            .add_node("slow", slow_node)
            .set_entry("slow")
            .build(runtime=self.server.runtime)
        )
        registry = AgentRegistry()
        registry.register(agent)
        self.server.registry = registry
        return release

    def _poll_status(self, run_id: str) -> tuple[int, object]:
        return self.request("GET", f"/api/runs/{run_id}/status")

    def _poll_until_terminal(self, run_id: str, timeout: float = 10.0) -> str:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, payload = self._poll_status(run_id)
            if status == 200 and payload["status"] not in {"PENDING", "RUNNING"}:
                return payload["status"]
            time.sleep(0.05)
        self.fail(f"run {run_id} did not reach a terminal status")

    def test_duplicate_active_run_id_returns_conflict(self) -> None:
        release = self._register_slow_agent()
        outcome: dict[str, object] = {}

        def execute() -> None:
            outcome["result"] = self.server.registry.get("slow-agent").run({}, run_id="duplicate-run")

        worker = threading.Thread(target=execute)
        worker.start()
        try:
            status, body = self.post_json(
                "/api/runs", {"agent": "slow-agent", "input": {}, "run_id": "duplicate-run"},
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "RUN_ALREADY_ACTIVE")
            status, body = self.request("POST", "/api/runs/duplicate-run/cancel")
            self.assertEqual(status, 200)
            self.assertIs(body["accepted"], True)
        finally:
            release.set()
            worker.join(timeout=5.0)
        self.assertEqual(outcome["result"].status.value, "CANCELED")

    def test_async_run_polls_to_completion(self) -> None:
        import time

        status, payload = self.post_json(
            "/api/runs",
            {"agent": "workbench", "input": {"task": "异步任务一;异步任务二"}, "wait": False},
        )
        self.assertEqual(status, 202)
        run_id = payload["run_id"]
        self.assertEqual(payload["status"], "PENDING")
        self.assertEqual(self._poll_until_terminal(run_id), "COMPLETED")
        status, payload = self.request("GET", f"/api/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["run"]["status"], "COMPLETED")
        # 终态可见与后台线程清理活跃集合之间存在短暂窗口,轮询等待清理完成
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            _, listing = self.request("GET", "/api/runs")
            if run_id not in listing["active"]:
                break
            time.sleep(0.05)
        self.assertNotIn(run_id, listing["active"])
        self.assertTrue(any(item["run_id"] == run_id for item in listing["runs"]))

    def test_async_run_rejects_reused_run_id(self) -> None:
        status, payload = self.post_json(
            "/api/runs", {"agent": "workbench", "input": {"task": "第一次"}, "run_id": "async-reuse", "wait": False},
        )
        self.assertEqual(status, 202)
        self.assertEqual(self._poll_until_terminal("async-reuse"), "COMPLETED")
        status, body = self.post_json(
            "/api/runs", {"agent": "workbench", "input": {"task": "第二次"}, "run_id": "async-reuse", "wait": False},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "RUN_ID_ALREADY_USED")

    def test_async_run_conflict_while_active(self) -> None:
        release = self._register_slow_agent()
        status, payload = self.post_json(
            "/api/runs", {"agent": "slow-agent", "input": {}, "run_id": "async-conflict", "wait": False},
        )
        self.assertEqual(status, 202)
        try:
            status, body = self.post_json(
                "/api/runs", {"agent": "slow-agent", "input": {}, "run_id": "async-conflict", "wait": False},
            )
            self.assertEqual(status, 409)
            self.assertEqual(body["error"]["code"], "RUN_ALREADY_ACTIVE")
        finally:
            release.set()
        self.assertEqual(self._poll_until_terminal("async-conflict"), "COMPLETED")

    def test_events_stream_pushes_lifecycle_and_done(self) -> None:
        release = self._register_slow_agent()
        status, payload = self.post_json(
            "/api/runs", {"agent": "slow-agent", "input": {}, "run_id": "sse-run", "wait": False},
        )
        self.assertEqual(status, 202)

        connection = HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request("GET", "/api/runs/sse-run/events/stream")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(response.getheader("Content-Type", "").startswith("text/event-stream"))

            frames: list[dict[str, str]] = []
            current: dict[str, str] = {}
            released = False
            while True:
                raw = response.readline()
                if not raw:
                    break
                line = raw.decode("utf-8").rstrip("\r\n")
                if not line:
                    if current:
                        frames.append(current)
                        current = {}
                    if frames and frames[-1]["event"] == "done":
                        break
                    continue
                if line.startswith(":"):
                    continue
                key, _, value = line.partition(": ")
                current[key] = value
                if key == "data" and current.get("event") == "lifecycle":
                    data = json.loads(value)
                    if data.get("to_status") == "RUNNING" and not released:
                        release.set()
                        released = True
        finally:
            release.set()
            connection.close()

        self.assertTrue(released)
        done_frames = [frame for frame in frames if frame["event"] == "done"]
        self.assertTrue(done_frames)
        self.assertEqual(json.loads(done_frames[-1]["data"])["status"], "COMPLETED")
        lifecycle = [json.loads(frame["data"]) for frame in frames if frame["event"] == "lifecycle"]
        event_types = [event["event_type"] for event in lifecycle]
        self.assertIn("run.created", event_types)
        self.assertIn("run.running", event_types)

    def test_events_stream_unknown_run_returns_json_error(self) -> None:
        status, body = self.request("GET", "/api/runs/missing-run/events/stream")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "UNKNOWN_RUN")

    def test_async_run_cooperative_cancel(self) -> None:
        import time

        release = self._register_slow_agent()
        status, payload = self.post_json(
            "/api/runs", {"agent": "slow-agent", "input": {}, "run_id": "async-cancel", "wait": False},
        )
        self.assertEqual(status, 202)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            _, payload = self._poll_status("async-cancel")
            if payload["status"] == "RUNNING":
                break
            time.sleep(0.02)
        try:
            status, body = self.request("POST", "/api/runs/async-cancel/cancel")
            self.assertEqual(status, 200)
            self.assertIs(body["accepted"], True)
        finally:
            release.set()
        self.assertEqual(self._poll_until_terminal("async-cancel"), "CANCELED")


class ServerResumeTests(ServerTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.register_recovery_agent()

    def test_blocked_run_resumes_with_state_updates(self) -> None:
        status, payload = self.post_json(
            "/api/runs", {"agent": "recovery-router", "input": {"task": "紧急修复线上问题"}},
        )
        self.assertEqual(status, 200)
        run = payload["run"]
        self.assertEqual(run["status"], "BLOCKED")
        self.assertEqual(run["reason_code"], "ROUTE_UNRESOLVED")
        self.assertIs(run["resumable"], True)

        status, payload = self.request(
            "POST", f"/api/runs/{run['run_id']}/resume",
            json.dumps({"state_updates": {"priority": "high"}}),
        )
        self.assertEqual(status, 200)
        resumed = payload["run"]
        self.assertEqual(resumed["status"], "COMPLETED")
        self.assertEqual(resumed["state"]["outcome"], "加急通道处理:紧急修复线上问题")
        self.assertIs(payload["state_updates_applied"], True)
        lifecycle_types = [event["event_type"] for event in resumed["lifecycle_events"]]
        self.assertIn("run.resumed", lifecycle_types)

    def test_resume_without_updates_reblocks_and_stays_resumable(self) -> None:
        status, payload = self.post_json(
            "/api/runs", {"agent": "recovery-router", "input": {"task": "普通任务"}},
        )
        run = payload["run"]
        self.assertEqual(run["status"], "BLOCKED")
        status, payload = self.request("POST", f"/api/runs/{run['run_id']}/resume", json.dumps({}))
        self.assertEqual(status, 200)
        self.assertEqual(payload["run"]["status"], "BLOCKED")
        self.assertIs(payload["run"]["resumable"], True)

    def test_resume_rejects_unresumable_runs(self) -> None:
        status, payload = self.post_json(
            "/api/runs", {"agent": "workbench", "input": {"task": "正常任务"}},
        )
        run_id = payload["run"]["run_id"]
        status, body = self.request("POST", f"/api/runs/{run_id}/resume", json.dumps({}))
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "NOT_RESUMABLE")

        status, payload = self.post_json("/api/runs", {"agent": "workbench", "input": {}})
        run_id = payload["run"]["run_id"]
        self.assertIs(payload["run"]["resumable"], False)
        status, body = self.request("POST", f"/api/runs/{run_id}/resume", json.dumps({}))
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "NOT_RESUMABLE")

    def test_checkpoint_inspection_endpoint(self) -> None:
        status, payload = self.post_json(
            "/api/runs", {"agent": "recovery-router", "input": {"task": "待检查任务"}},
        )
        run_id = payload["run"]["run_id"]
        status, payload = self.request("GET", f"/api/runs/{run_id}/checkpoint")
        self.assertEqual(status, 200)
        checkpoint = payload["checkpoint"]
        self.assertEqual(checkpoint["next_node"], "route")
        self.assertEqual(checkpoint["state"]["task"], "待检查任务")
        self.assertEqual(payload["status"], "BLOCKED")

        status, body = self.request("GET", "/api/runs/missing-run/checkpoint")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "UNKNOWN_CHECKPOINT")

    def test_resume_validates_request(self) -> None:
        status, body = self.request("POST", "/api/runs/missing-run/resume", json.dumps({}))
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "UNKNOWN_RUN")

        status, payload = self.post_json(
            "/api/runs", {"agent": "recovery-router", "input": {"task": "待恢复任务"}},
        )
        run_id = payload["run"]["run_id"]
        status, body = self.request(
            "POST", f"/api/runs/{run_id}/resume", json.dumps({"state_updates": "high"}),
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_STATE_UPDATES")
        status, body = self.request(
            "POST", f"/api/runs/{run_id}/resume",
            json.dumps({"state_updates": {"priority": {"nested": "ok"}}}),
        )
        self.assertEqual(status, 200)


class ServerSessionTests(ServerTestBase):
    def test_session_memory_accumulates_across_runs(self) -> None:
        status, first = self.post_json(
            "/api/runs",
            {"agent": "workbench", "input": {"task": "会话任务一"}, "session_id": "sess-demo"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(first["run"]["state"]["session_task_count"], 1)
        self.assertIn("本会话第 1 个任务", first["run"]["state"]["summary"])
        self.assertEqual(first["session"]["id"], "sess-demo")

        status, second = self.post_json(
            "/api/runs",
            {"agent": "workbench", "input": {"task": "会话任务二"}, "session_id": "sess-demo"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(second["run"]["state"]["session_task_count"], 2)

        status, payload = self.request("GET", "/api/sessions/sess-demo")
        self.assertEqual(status, 200)
        memory = payload["memory"]
        self.assertEqual(memory["last_task"], "会话任务二")
        self.assertEqual(len(memory["processed_tasks"]), 2)

        status, standalone = self.post_json(
            "/api/runs", {"agent": "workbench", "input": {"task": "无会话任务"}},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("session_task_count", standalone["run"]["state"])
        self.assertNotIn("本会话", standalone["run"]["state"]["summary"])
        self.assertNotIn("session", standalone)

    def test_session_listing_and_clear(self) -> None:
        self.post_json(
            "/api/runs",
            {"agent": "workbench", "input": {"task": "会话记录"}, "session_id": "sess-list"},
        )
        status, payload = self.request("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertIn("sess-list", payload["sessions"])

        status, payload = self.request("DELETE", "/api/sessions/sess-list")
        self.assertEqual(status, 200)
        self.assertIs(payload["cleared"], True)
        status, _ = self.request("GET", "/api/sessions/sess-list")
        self.assertEqual(status, 404)
        status, _ = self.request("DELETE", "/api/sessions/sess-list")
        self.assertEqual(status, 404)

    def test_session_request_validation(self) -> None:
        status, body = self.post_json(
            "/api/runs",
            {"agent": "workbench", "input": {"task": "x"}, "session_id": "bad id!"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_SESSION")
        status, body = self.request("GET", "/api/sessions/missing-session")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "UNKNOWN_SESSION")


class ServerRunIndexTests(ServerTestBase):
    def test_list_runs_pagination_filter_and_order(self) -> None:
        # 先创建阻断运行,让最新的两条是带 summary 的完成运行
        self.post_json("/api/runs", {"agent": "workbench", "input": {}})
        self.post_json("/api/runs", {"agent": "workbench", "input": {"task": "任务一;步骤"}})
        self.post_json("/api/runs", {"agent": "workbench", "input": {"task": "任务二;步骤"}})

        status, payload = self.request("GET", "/api/runs?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 3)
        self.assertEqual(len(payload["runs"]), 2)
        self.assertEqual(payload["offset"], 0)
        started = [item["started_at"] for item in payload["runs"]]
        self.assertEqual(started, sorted(started, reverse=True))
        self.assertTrue(all(item["agent"] == "workbench" for item in payload["runs"]))
        self.assertIn("已完成对", payload["runs"][0]["summary"])

        status, payload = self.request("GET", "/api/runs?limit=2&offset=2")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["runs"]), 1)

        status, payload = self.request("GET", "/api/runs?status=BLOCKED")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["runs"][0]["status"], "BLOCKED")
        self.assertEqual(payload["runs"][0]["reason_code"], "EMPTY_TASK")

        status, body = self.request("GET", "/api/runs?limit=0")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_QUERY")
        status, body = self.request("GET", "/api/runs?status=NOPE")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_QUERY")

    def test_resume_after_server_restart(self) -> None:
        from agent_core.server import create_server, register_agents_from_config

        self.register_recovery_agent()
        status, payload = self.post_json(
            "/api/runs", {"agent": "recovery-router", "input": {"task": "重启前的阻断任务"}},
        )
        self.assertEqual(status, 200)
        run = payload["run"]
        self.assertEqual(run["status"], "BLOCKED")

        # 模拟重启:同一存储目录上的全新服务实例,依赖持久化的归属索引
        restarted = create_server(
            port=0, store_dir=Path(self._tmp.name) / "runs", frontend_dir=None,
        )
        register_agents_from_config(
            "examples/recovery_agent.json", restarted.registry, runtime=restarted.runtime,
        )
        thread = threading.Thread(target=restarted.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", restarted.server_address[1], timeout=10)
            connection.request(
                "POST", f"/api/runs/{run['run_id']}/resume",
                body=json.dumps({"state_updates": {"priority": "high"}}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            body = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(body["run"]["status"], "COMPLETED")
            self.assertEqual(body["run"]["state"]["outcome"], "加急通道处理:重启前的阻断任务")
        finally:
            restarted.shutdown()
            restarted.server_close()
            thread.join(timeout=5.0)


class ServerConfigReloadTests(ServerTestBase):
    def _hot_config(self, version: int) -> str:
        return json.dumps({
            "name": "hot-agent",
            "description": f"v{version}",
            "entry": "only",
            "nodes": [{"id": "only", "type": "template", "params": {"updates": {"v": f"v{version}"}}}],
        }, ensure_ascii=False)

    def test_config_hot_reload_replaces_and_unregisters(self) -> None:
        config_path = Path(self._tmp.name) / "hot.json"
        config_path.write_text(self._hot_config(1), encoding="utf-8")
        registered = register_agents_from_config(config_path, self.server.registry, runtime=self.server.runtime)
        self.server.note_config_ownership(str(config_path), registered)

        status, payload = self.post_json("/api/runs", {"agent": "hot-agent", "input": {}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["run"]["state"]["v"], "v1")

        config_path.write_text(self._hot_config(2), encoding="utf-8")
        self.assertEqual(self.server.reload_config(config_path), ["hot-agent"])
        status, payload = self.request("GET", "/api/agents")
        hot = [item for item in payload["agents"] if item["name"] == "hot-agent"][0]
        self.assertEqual(hot["description"], "v2")
        status, payload = self.post_json("/api/runs", {"agent": "hot-agent", "input": {}})
        self.assertEqual(payload["run"]["state"]["v"], "v2")

        config_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.server.reload_config(config_path)
        status, payload = self.post_json("/api/runs", {"agent": "hot-agent", "input": {}})
        self.assertEqual(payload["run"]["state"]["v"], "v2")

        config_path.write_text(json.dumps({"agents": []}), encoding="utf-8")
        self.assertEqual(self.server.reload_config(config_path), [])
        status, payload = self.request("GET", "/api/health")
        self.assertNotIn("hot-agent", payload["agents"])

    def test_orchestrator_delegates_via_api(self) -> None:
        register_agents_from_config(
            "examples/support_agent.json", self.server.registry, runtime=self.server.runtime,
        )
        register_agents_from_config(
            "examples/orchestrator_agent.json", self.server.registry, runtime=self.server.runtime,
        )
        status, payload = self.post_json(
            "/api/runs", {"agent": "orchestrator", "input": {"task": "无法登录;收不到验证码"}},
        )
        self.assertEqual(status, 200)
        run = payload["run"]
        self.assertEqual(run["status"], "COMPLETED")
        self.assertEqual(run["state"]["delegated"]["status"], "COMPLETED")
        self.assertIn("已拆解为 2 个步骤", run["state"]["delegated_summary"])
        self.assertEqual(run["state"]["summary"], "委派完成:工单“无法登录;收不到验证码”已拆解为 2 个步骤。")
        self.assertNotEqual(run["state"]["delegated"]["run_id"], run["run_id"])
        self.assertEqual(run["trace"][-1]["event_type"], "run.completed")

    def test_config_dependency_cycle_rejected(self) -> None:
        config_path = Path(self._tmp.name) / "cycle.json"
        config_path.write_text(json.dumps({"agents": [
            {
                "name": "agent-a", "entry": "call",
                "nodes": [{"id": "call", "type": "subagent", "params": {"agent": "agent-b"}}],
            },
            {
                "name": "agent-b", "entry": "call",
                "nodes": [{"id": "call", "type": "subagent", "params": {"agent": "agent-a"}}],
            },
        ]}, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError):
            register_agents_from_config(config_path, self.server.registry, runtime=self.server.runtime)
        status, payload = self.request("GET", "/api/health")
        self.assertEqual(payload["agents"], ["workbench"])


if __name__ == "__main__":
    unittest.main()

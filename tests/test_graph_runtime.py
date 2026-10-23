"""v0.1 Graph Runtime contract tests."""

import unittest

from agent_core import (
    Graph,
    NodeResult,
    NodeStatus,
    RunStatus,
    Runtime,
    Tool,
    ToolRegistry,
    graph_tool_node,
    tool_step,
)


class GraphRuntimeTests(unittest.TestCase):
    def test_graph_can_reuse_tool_step_boundary(self) -> None:
        registry = ToolRegistry()
        registry.register(Tool("echo", lambda args: {"echo": args["value"]}))

        graph = Graph().add_node(
            "call", graph_tool_node("echo", registry, lambda ctx: {"value": ctx.input["value"]})
        ).set_entry("call").compile()
        result = Runtime().run(graph, {"value": "hello"})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state["echo"], "hello")

    def test_graph_tool_boundary_preserves_denial_reason(self) -> None:
        registry = ToolRegistry()
        registry.register(
            Tool("guarded", lambda args: {"ok": True}, required_capabilities=frozenset({"read"}))
        )
        graph = Graph().add_node(
            "call", graph_tool_node("guarded", registry, lambda ctx: {})
        ).set_entry("call").compile()
        result = Runtime().run(graph, {})
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "CAPABILITY_DENIED")

    def test_graph_tool_failure_and_missing_tool_stop_downstream(self) -> None:
        for tool_name, expected_status, expected_reason in (
            ("missing", RunStatus.BLOCKED, "TOOL_NOT_FOUND"),
            ("boom", RunStatus.FAILED, "TOOL_EXCEPTION"),
        ):
            registry = ToolRegistry()
            if tool_name == "boom":
                registry.register(Tool("boom", lambda args: (_ for _ in ()).throw(RuntimeError("secret"))))
            calls: list[str] = []
            graph = (
                Graph()
                .add_node("call", graph_tool_node(tool_name, registry, lambda ctx: {}))
                .add_node("downstream", lambda ctx: (calls.append("downstream") or NodeResult.success()))
                .set_entry("call")
                .add_edge("call", "downstream")
                .compile()
            )
            result = Runtime().run(graph, {})
            self.assertEqual(result.status, expected_status)
            self.assertEqual(result.reason_code, expected_reason)
            self.assertEqual(calls, [])
            self.assertNotIn("secret", str(result.trace))

    def test_default_edge_matches_only_without_a_route(self) -> None:
        calls: list[str] = []
        graph = (
            Graph()
            .add_node("start", lambda ctx: NodeResult.success())
            .add_node("finish", lambda ctx: (calls.append("finish") or NodeResult.success()))
            .set_entry("start")
            .add_edge("start", "finish")
            .compile()
        )
        result = Runtime().run(graph, {})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(calls, ["finish"])

    def test_unreachable_nodes_are_rejected(self) -> None:
        graph = (
            Graph()
            .add_node("entry", lambda ctx: NodeResult.success())
            .add_node("orphan", lambda ctx: NodeResult.success())
            .set_entry("entry")
        )
        with self.assertRaises(ValueError):
            graph.compile()

    def test_compile_does_not_execute_nodes(self) -> None:
        calls: list[str] = []

        def node(ctx):
            calls.append("executed")
            return NodeResult.success()

        Graph().add_node("node", node).set_entry("node").compile()
        self.assertEqual(calls, [])

    def test_runtime_requires_compiled_graph_and_positive_step_limit(self) -> None:
        with self.assertRaises(ValueError):
            Runtime(max_steps=0)
        with self.assertRaises(ValueError):
            Runtime(max_steps=True)
        with self.assertRaises(TypeError):
            Runtime().run(Graph(), {})

    def test_multiple_default_edges_are_rejected_at_compile(self) -> None:
        graph = (
            Graph()
            .add_node("a", lambda ctx: NodeResult.success())
            .add_node("b", lambda ctx: NodeResult.success())
            .add_node("c", lambda ctx: NodeResult.success())
            .set_entry("a")
            .add_edge("a", "b")
            .add_edge("a", "c")
        )
        with self.assertRaisesRegex(ValueError, "multiple default edges"):
            graph.compile()

    def test_node_result_rejects_invalid_status_combinations(self) -> None:
        with self.assertRaises(TypeError):
            NodeResult(status="SUCCEEDED")
        with self.assertRaises(ValueError):
            NodeResult(NodeStatus.SUCCEEDED, reason_code="unexpected")
        with self.assertRaises(ValueError):
            NodeResult(NodeStatus.BLOCKED)
        with self.assertRaises(ValueError):
            NodeResult(NodeStatus.FAILED, route="next", reason_code="failed")

    def test_runtime_saves_checkpoint_after_successful_boundary(self) -> None:
        from agent_core import InMemoryCheckpointStore

        store = InMemoryCheckpointStore()
        graph = Graph().add_node("done", lambda ctx: NodeResult.success({"done": True}))\
            .set_entry("done").compile()
        result = Runtime(checkpoint_store=store, graph_id="graph-v1").run(graph, {}, run_id="checkpointed")
        checkpoint = store.load("checkpointed")
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(checkpoint.next_node, None)
        self.assertEqual(checkpoint.state, {"done": True})
        self.assertEqual(checkpoint.state_version, 1)
        self.assertEqual(checkpoint.step_count, 1)

    def test_runtime_file_checkpoint_roundtrip(self) -> None:
        import tempfile
        from pathlib import Path
        from agent_core import FileCheckpointStore

        with tempfile.TemporaryDirectory() as directory:
            store = FileCheckpointStore(Path(directory))
            graph = Graph().add_node(
                "done", lambda ctx: NodeResult.success({"done": True})
            ).set_entry("done").compile()
            result = Runtime(checkpoint_store=store, graph_id="graph-v1").run(
                graph, {}, run_id="file-checkpoint"
            )
            loaded = FileCheckpointStore(Path(directory)).load_for(
                "file-checkpoint", "graph-v1"
            )
            self.assertEqual(result.status, RunStatus.COMPLETED)
            self.assertEqual(loaded.state, {"done": True})
            self.assertEqual(loaded.state_version, 1)
            self.assertEqual(loaded.step_count, 1)

    def test_runtime_checkpoint_records_next_node_on_route(self) -> None:
        from agent_core import InMemoryCheckpointStore

        store = InMemoryCheckpointStore()
        saved = []
        original_save = store.save

        def record(checkpoint):
            saved.append(checkpoint)
            original_save(checkpoint)

        store.save = record
        graph = (
            Graph()
            .add_node("first", lambda ctx: NodeResult.success({"value": 1}, route="next"))
            .add_node("second", lambda ctx: NodeResult.success({"done": True}))
            .set_entry("first")
            .add_edge("first", "second", when="next")
            .compile()
        )
        result = Runtime(checkpoint_store=store, graph_id="graph-v1").run(
            graph, {}, run_id="routed-checkpoint"
        )
        checkpoint = store.load("routed-checkpoint")
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(checkpoint.next_node, None)
        self.assertEqual(checkpoint.state, {"value": 1, "done": True})
        self.assertEqual(checkpoint.state_version, 2)
        self.assertEqual(checkpoint.step_count, 2)
        self.assertEqual([(item.next_node, item.state_version, item.step_count) for item in saved],
                         [("second", 1, 1), (None, 2, 2)])

    def test_runtime_resume_continues_from_checkpoint_boundary(self) -> None:
        from agent_core import Checkpoint, InMemoryCheckpointStore

        calls: list[str] = []
        graph = (
            Graph()
            .add_node("first", lambda ctx: (calls.append("first") or NodeResult.success({"a": 1}, route="next")))
            .add_node("second", lambda ctx: (calls.append("second") or NodeResult.success({"b": 2})))
            .set_entry("first").add_edge("first", "second", when="next").compile()
        )
        checkpoint = Checkpoint("resume-1", "graph-v1", "second", {"a": 1}, 1, 1)
        result = Runtime(graph_id="graph-v1").resume(graph, checkpoint)
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state, {"a": 1, "b": 2})
        self.assertEqual(calls, ["second"])
        self.assertTrue(result.trace)
        self.assertTrue(all(event.run_id == "resume-1" for event in result.trace))
        started = next(event for event in result.trace if event.event_type == "node.started")
        committed = next(event for event in result.trace if event.event_type == "state.committed")
        self.assertEqual(started.state_version, 1)
        self.assertEqual(committed.state_version, 2)

    def test_runtime_exposes_lifecycle_status_and_events(self) -> None:
        graph = Graph().add_node("done", lambda ctx: NodeResult.success()).set_entry("done").compile()
        runtime = Runtime()
        result = runtime.run(graph, {}, run_id="lifecycle-run")
        self.assertEqual(result.status, RunStatus.COMPLETED)
        from agent_core import LifecycleStatus
        self.assertEqual(runtime.get_status("lifecycle-run"), LifecycleStatus.COMPLETED)
        self.assertEqual(
            [event.event_type for event in runtime.get_lifecycle_events("lifecycle-run")],
            ["run.created", "run.running", "run.completed"],
        )
        self.assertIsNone(runtime.get_status("unknown-run"))

    def test_cancel_request_stops_before_next_node(self) -> None:
        calls: list[str] = []
        runtime = Runtime()

        def first(ctx):
            calls.append("first")
            runtime.cancel(ctx.run_id)
            return NodeResult.success({}, route="next")

        graph = (
            Graph().add_node("first", first)
            .add_node("second", lambda ctx: calls.append("second") or NodeResult.success())
            .set_entry("first").add_edge("first", "second", when="next").compile()
        )
        result = runtime.run(graph, {}, run_id="cancel-run")
        from agent_core import LifecycleStatus
        self.assertEqual(result.status, RunStatus.CANCELED)
        self.assertEqual(result.reason_code, "CANCEL_REQUESTED")
        self.assertEqual(calls, ["first"])
        self.assertEqual(runtime.get_status("cancel-run"), LifecycleStatus.CANCELED)
        self.assertEqual(result.trace[-1].event_type, "run.canceled")
        self.assertFalse(runtime.cancel("cancel-run"))

    def test_timeout_stops_at_node_boundary_as_canceled(self) -> None:
        import time

        calls: list[str] = []
        graph = (
            Graph()
            .add_node("slow", lambda ctx: (calls.append("slow") or time.sleep(0.01) or NodeResult.success({}, route="next")))
            .add_node("second", lambda ctx: calls.append("second") or NodeResult.success())
            .set_entry("slow").add_edge("slow", "second", when="next").compile()
        )
        runtime = Runtime(timeout_seconds=0.001)
        result = runtime.run(graph, {}, run_id="timeout-run")
        from agent_core import LifecycleStatus
        self.assertEqual(result.status, RunStatus.CANCELED)
        self.assertEqual(result.reason_code, "TIMEOUT")
        self.assertEqual(calls, ["slow"])
        self.assertEqual(runtime.get_status("timeout-run"), LifecycleStatus.CANCELED)
        self.assertEqual(result.trace[-1].event_type, "run.canceled")

    def test_timeout_must_be_positive_when_provided(self) -> None:
        with self.assertRaises(ValueError):
            Runtime(timeout_seconds=0)
        with self.assertRaises(ValueError):
            Runtime(timeout_seconds=True)

    def test_runtime_records_blocked_and_failed_lifecycle_events(self) -> None:
        cases = (
            ("blocked", NodeResult.blocked("WAITING"), "run.blocked"),
            ("failed", NodeResult.failed("BROKEN"), "run.failed"),
        )
        from agent_core import LifecycleStatus
        for run_id, node_result, event_type in cases:
            with self.subTest(run_id=run_id):
                graph = Graph().add_node("stop", lambda ctx, value=node_result: value).set_entry("stop").compile()
                runtime = Runtime()
                result = runtime.run(graph, {}, run_id=run_id)
                self.assertEqual(result.status.value, run_id.upper())
                self.assertEqual(result.lifecycle_events[-1].event_type, event_type)
                self.assertEqual(runtime.get_status(run_id).value, run_id.upper())

    def test_runtime_store_persists_and_queries_lifecycle_across_instances(self) -> None:
        import tempfile
        from pathlib import Path
        from agent_core import RunStore

        graph = Graph().add_node("done", lambda ctx: NodeResult.success()).set_entry("done").compile()
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            result = Runtime(store=store).run(graph, {}, run_id="stored-lifecycle")
            self.assertEqual(result.status, RunStatus.COMPLETED)
            fresh_runtime = Runtime(store=store)
            from agent_core import LifecycleStatus
            self.assertEqual(fresh_runtime.get_status("stored-lifecycle"), LifecycleStatus.COMPLETED)
            self.assertEqual(
                [event.event_type for event in fresh_runtime.get_lifecycle_events("stored-lifecycle")],
                ["run.created", "run.running", "run.completed"],
            )

    def test_runtime_store_failure_is_reported_after_execution(self) -> None:
        class BrokenStore:
            def save(self, result):
                raise OSError("storage unavailable")

            def load(self, run_id):
                return None

        graph = Graph().add_node("done", lambda ctx: NodeResult.success()).set_entry("done").compile()
        runtime = Runtime(store=BrokenStore())
        with self.assertRaisesRegex(OSError, "storage unavailable"):
            runtime.run(graph, {}, run_id="store-failure")
        from agent_core import LifecycleStatus
        self.assertEqual(runtime.get_status("store-failure"), LifecycleStatus.COMPLETED)

    def test_runtime_store_exposes_running_lifecycle_to_fresh_instance(self) -> None:
        import tempfile
        import threading
        from pathlib import Path
        from agent_core import LifecycleStatus, RunStore

        started = threading.Event()
        release = threading.Event()
        graph = Graph().add_node(
            "slow",
            lambda ctx: (started.set() or release.wait(1) or NodeResult.success()),
        ).set_entry("slow").compile()
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            runtime = Runtime(store=store)
            worker = threading.Thread(target=lambda: runtime.run(graph, {}, run_id="running-query"))
            worker.start()
            self.assertTrue(started.wait(1))
            fresh = Runtime(store=store)
            self.assertEqual(fresh.get_status("running-query"), LifecycleStatus.RUNNING)
            self.assertEqual(
                [event.event_type for event in fresh.get_lifecycle_events("running-query")],
                ["run.created", "run.running"],
            )
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def test_resume_preserves_persisted_lifecycle_history(self) -> None:
        import tempfile
        from pathlib import Path
        from agent_core import Checkpoint, RunStore

        graph = (
            Graph()
            .add_node("first", lambda ctx: NodeResult.success({"a": 1}, route="next"))
            .add_node("second", lambda ctx: NodeResult.success({"b": 2}))
            .set_entry("first").add_edge("first", "second", when="next").compile()
        )
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            Runtime(store=store, max_steps=1).run(graph, {}, run_id="history-resume")
            checkpoint = Checkpoint("history-resume", "graph", "second", {"a": 1}, 1, 1)
            result = Runtime(store=store, max_steps=2).resume(graph, checkpoint)
            self.assertEqual(
                [event.event_type for event in result.lifecycle_events],
                ["run.created", "run.running", "run.failed", "run.resumed", "run.completed"],
            )

    def test_resume_recovers_persisted_running_lifecycle(self) -> None:
        import tempfile
        from pathlib import Path
        from agent_core import Checkpoint, LifecycleEvent, LifecycleStatus, RunStore
        from datetime import datetime, timezone

        graph = Graph().add_node("done", lambda ctx: NodeResult.success()).set_entry("done").compile()
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            created = LifecycleEvent("crashed-run", 1, "e1", "run.created", None,
                                     LifecycleStatus.PENDING, datetime.now(timezone.utc).isoformat())
            running = LifecycleEvent("crashed-run", 2, "e2", "run.running",
                                     LifecycleStatus.PENDING, LifecycleStatus.RUNNING,
                                     datetime.now(timezone.utc).isoformat())
            store.save_lifecycle_events("crashed-run", (created, running))
            checkpoint = Checkpoint("crashed-run", "graph", "done", {}, 0, 0)
            result = Runtime(store=store).resume(graph, checkpoint)
            self.assertEqual(result.status, RunStatus.COMPLETED)
            self.assertEqual(
                [event.event_type for event in result.lifecycle_events],
                ["run.created", "run.running", "run.recovered", "run.completed"],
            )

    def test_external_thread_can_request_cooperative_cancellation(self) -> None:
        import threading

        started = threading.Event()
        release = threading.Event()
        holder: dict[str, object] = {}
        runtime = Runtime()

        def slow(ctx):
            started.set()
            release.wait(1)
            return NodeResult.success()

        graph = Graph().add_node("slow", slow).set_entry("slow").compile()

        def execute() -> None:
            holder["result"] = runtime.run(graph, {}, run_id="thread-cancel")

        worker = threading.Thread(target=execute)
        worker.start()
        self.assertTrue(started.wait(1))
        from agent_core import LifecycleStatus
        self.assertEqual(runtime.get_status("thread-cancel"), LifecycleStatus.RUNNING)
        self.assertTrue(runtime.cancel("thread-cancel"))
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(holder["result"].status, RunStatus.CANCELED)

    def test_resume_rejects_incompatible_or_terminal_checkpoint(self) -> None:
        from agent_core import Checkpoint

        graph = Graph().add_node("done", lambda ctx: NodeResult.success()).set_entry("done").compile()
        runtime = Runtime(graph_id="graph-v1")
        with self.assertRaises(ValueError):
            runtime.resume(graph, Checkpoint("r1", "graph-v2", "done", {}, 0, 0))
        with self.assertRaises(ValueError):
            runtime.resume(graph, Checkpoint("r2", "graph-v1", None, {}, 1, 1))
        with self.assertRaisesRegex(ValueError, "next_node does not exist"):
            runtime.resume(graph, Checkpoint("r3", "graph-v1", "missing", {}, 1, 1))

    def test_resume_from_file_checkpoint_updates_persisted_boundary(self) -> None:
        import tempfile
        from pathlib import Path
        from agent_core import FileCheckpointStore

        graph = (
            Graph()
            .add_node("first", lambda ctx: NodeResult.success({"a": 1}, route="next"))
            .add_node("second", lambda ctx: NodeResult.success({"b": 2}))
            .set_entry("first").add_edge("first", "second", when="next").compile()
        )
        with tempfile.TemporaryDirectory() as directory:
            store = FileCheckpointStore(Path(directory))
            first_runtime = Runtime(graph_id="graph-v1", checkpoint_store=store, max_steps=1)
            first = first_runtime.run(graph, {}, run_id="persisted-resume")
            self.assertEqual(first.status, RunStatus.FAILED)
            boundary = store.load("persisted-resume")
            self.assertIsNotNone(boundary)
            self.assertEqual(boundary.next_node, "second")
            self.assertEqual(boundary.step_count, 1)

            resumed = Runtime(graph_id="graph-v1", checkpoint_store=store, max_steps=2).resume(
                graph, boundary
            )
            self.assertEqual(resumed.status, RunStatus.COMPLETED)
            self.assertEqual(resumed.state, {"a": 1, "b": 2})
            final_checkpoint = store.load("persisted-resume")
            self.assertIsNotNone(final_checkpoint)
            self.assertIsNone(final_checkpoint.next_node)
            self.assertEqual(final_checkpoint.step_count, 2)

    def test_resume_respects_remaining_step_budget(self) -> None:
        from agent_core import Checkpoint

        calls: list[str] = []
        graph = (
            Graph()
            .add_node("second", lambda ctx: (calls.append("second") or NodeResult.success({"b": 2})))
            .set_entry("second").compile()
        )
        checkpoint = Checkpoint("budget-1", "graph-v1", "second", {"a": 1}, 1, 1)
        result = Runtime(graph_id="graph-v1", max_steps=1).resume(graph, checkpoint)
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "MAX_STEPS_EXCEEDED")
        self.assertEqual(calls, [])
        self.assertEqual(result.trace[-1].event_type, "run.failed")
        self.assertEqual(result.trace[-1].reason_code, "MAX_STEPS_EXCEEDED")

    def test_checkpoint_store_failure_fails_run_without_retry(self) -> None:
        class BrokenStore:
            def save(self, checkpoint):
                raise OSError("storage unavailable")
            def load(self, run_id):
                return None
            def delete(self, run_id):
                pass

        calls = []
        graph = Graph().add_node(
            "only", lambda ctx: (calls.append("only") or NodeResult.success())
        ).set_entry("only").compile()
        result = Runtime(checkpoint_store=BrokenStore()).run(graph, {})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "CHECKPOINT_SAVE_FAILED")
        self.assertEqual(calls, ["only"])

    def test_linear_graph_executes_in_order_and_merges_updates(self) -> None:
        calls: list[str] = []

        def first(ctx):
            calls.append("first")
            return NodeResult.success({"a": 1}, route="next")

        def second(ctx):
            calls.append("second")
            return NodeResult.success({"b": ctx.state["a"] + 1})

        graph = (
            Graph().add_node("first", first).add_node("second", second)
            .set_entry("first").add_edge("first", "second", when="next")
            .compile()
        )
        result = Runtime().run(graph, {"input": "x"}, run_id="linear")
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(result.state, {"input": "x", "a": 1, "b": 2})

    def test_conditional_route_runs_only_matching_branch(self) -> None:
        calls: list[str] = []

        graph = (
            Graph()
            .add_node("choose", lambda ctx: NodeResult.success(route="yes"))
            .add_node("yes", lambda ctx: (calls.append("yes") or NodeResult.success({"choice": "yes"})))
            .add_node("no", lambda ctx: (calls.append("no") or NodeResult.success({"choice": "no"})))
            .set_entry("choose")
            .add_edge("choose", "yes", when="yes")
            .add_edge("choose", "no", when="no")
            .compile()
        )
        result = Runtime().run(graph, {})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(calls, ["yes"])
        self.assertEqual(result.state["choice"], "yes")

    def test_blocked_and_failed_nodes_stop_downstream(self) -> None:
        for node_result, expected_status, reason in (
            (NodeResult.blocked("REVIEW_REQUIRED"), RunStatus.BLOCKED, "REVIEW_REQUIRED"),
            (NodeResult.failed("NODE_FAILURE"), RunStatus.FAILED, "NODE_FAILURE"),
        ):
            calls: list[str] = []
            graph = (
                Graph()
                .add_node("gate", lambda ctx, r=node_result: r)
                .add_node("downstream", lambda ctx: (calls.append("downstream") or NodeResult.success()))
                .set_entry("gate").add_edge("gate", "downstream").compile()
            )
            result = Runtime().run(graph, {})
            self.assertEqual(result.status, expected_status)
            self.assertEqual(result.reason_code, reason)
            self.assertEqual(calls, [])

    def test_blocked_and_failed_updates_are_not_committed(self) -> None:
        for node_result, expected_status in (
            (NodeResult(NodeStatus.BLOCKED, {"leak": True}, reason_code="STOP"), RunStatus.BLOCKED),
            (NodeResult(NodeStatus.FAILED, {"leak": True}, reason_code="ERROR"), RunStatus.FAILED),
        ):
            graph = Graph().add_node(
                "stop", lambda ctx, result=node_result: result
            ).set_entry("stop").compile()
            result = Runtime().run(graph, {"input": "original"})
            self.assertEqual(result.status, expected_status)
            self.assertEqual(result.state, {"input": "original"})

    def test_node_exception_is_failed_without_downstream_execution(self) -> None:
        calls: list[str] = []

        def explode(ctx):
            raise RuntimeError("hidden diagnostic")

        graph = (
            Graph().add_node("explode", explode)
            .add_node("downstream", lambda ctx: (calls.append("downstream") or NodeResult.success()))
            .set_entry("explode").add_edge("explode", "downstream").compile()
        )
        result = Runtime().run(graph, {})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "NODE_EXCEPTION")
        self.assertEqual(calls, [])
        self.assertNotIn("hidden diagnostic", str(result.trace))

    def test_invalid_graph_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Graph().add_node("a", lambda ctx: NodeResult.success()).set_entry("a").add_edge("a", "missing").compile()
        with self.assertRaises(ValueError):
            Graph().add_node("a", lambda ctx: NodeResult.success()).add_node("a", lambda ctx: NodeResult.success())

        graph = (
            Graph().add_node("a", lambda ctx: NodeResult.success())
            .add_node("b", lambda ctx: NodeResult.success())
            .set_entry("a")
            .add_edge("a", "b", when="next")
        )
        with self.assertRaises(ValueError):
            graph.add_edge("a", "b", when="next")

    def test_compiled_graph_is_an_immutable_snapshot(self) -> None:
        graph = Graph().add_node("a", lambda ctx: NodeResult.success()).set_entry("a")
        compiled = graph.compile()
        graph.add_node("b", lambda ctx: NodeResult.success())
        graph.add_edge("a", "b")

        self.assertEqual(tuple(compiled.nodes), ("a",))
        with self.assertRaises(TypeError):
            compiled.nodes["b"] = compiled.nodes["a"]

    def test_loop_is_bounded(self) -> None:
        graph = (
            Graph().add_node("loop", lambda ctx: NodeResult.success(route="again"))
            .set_entry("loop").add_edge("loop", "loop", when="again").compile()
        )
        result = Runtime(max_steps=3).run(graph, {})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "MAX_STEPS_EXCEEDED")

    def test_ambiguous_and_missing_routes_are_stable_failures(self) -> None:
        ambiguous = (
            Graph().add_node("a", lambda ctx: NodeResult.success(route="x"))
            .add_node("b", lambda ctx: NodeResult.success())
            .add_node("c", lambda ctx: NodeResult.success())
            .set_entry("a").add_edge("a", "b", when="x").add_edge("a", "c", when="x").compile()
        )
        self.assertEqual(Runtime().run(ambiguous, {}).reason_code, "AMBIGUOUS_ROUTE")

        missing = (
            Graph().add_node("a", lambda ctx: NodeResult.success(route="missing"))
            .add_node("b", lambda ctx: NodeResult.success())
            .set_entry("a").add_edge("a", "b", when="other").compile()
        )
        self.assertEqual(Runtime().run(missing, {}).reason_code, "NO_ROUTE")

    def test_trace_has_ordered_run_node_and_execution_identity(self) -> None:
        graph = Graph().add_node("one", lambda ctx: NodeResult.success()).set_entry("one").compile()
        result = Runtime().run(graph, {})
        self.assertTrue(result.run_id)
        self.assertTrue(result.trace)
        self.assertEqual([event.sequence for event in result.trace], list(range(1, len(result.trace) + 1)))
        self.assertTrue(all(event.run_id == result.run_id for event in result.trace))
        node_events = [event for event in result.trace if event.event_type.startswith("node.")]
        self.assertTrue(node_events)
        self.assertTrue(all(event.execution_id for event in node_events))

    def test_trace_state_versions_follow_successful_commits(self) -> None:
        graph = (
            Graph()
            .add_node("a", lambda ctx: NodeResult.success({"a": 1}, route="next"))
            .add_node("b", lambda ctx: NodeResult.success({"b": 2}))
            .set_entry("a")
            .add_edge("a", "b", when="next")
            .compile()
        )
        result = Runtime().run(graph, {})
        commits = [event for event in result.trace if event.event_type == "state.committed"]
        self.assertEqual([event.state_version for event in commits], [1, 2])
        self.assertEqual(result.trace[0].state_version, 0)

    def test_simulated_success_remains_marked(self) -> None:
        graph = Graph().add_node(
            "demo", lambda ctx: NodeResult.success(simulated=True)
        ).set_entry("demo").compile()
        result = Runtime().run(graph, {})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertTrue(result.simulated)

    def test_state_snapshot_cannot_mutate_runtime_state(self) -> None:
        def mutate(ctx):
            snapshot = ctx.state
            with self.assertRaises(TypeError):
                snapshot["input"] = "changed"
            return NodeResult.success()

        graph = Graph().add_node("mutate", mutate).set_entry("mutate").compile()
        result = Runtime().run(graph, {"input": "original"})
        self.assertEqual(result.state["input"], "original")

    def test_context_input_is_read_only_and_versions_are_visible(self) -> None:
        observed: list[tuple[str, int]] = []

        def first(ctx):
            observed.append((ctx.input["value"], ctx.state_version))
            with self.assertRaises(TypeError):
                ctx.input["value"] = "changed"
            return NodeResult.success({"step": "first"}, route="next")

        def second(ctx):
            observed.append((ctx.state["step"], ctx.state_version))
            return NodeResult.success()

        graph = (
            Graph().add_node("first", first).add_node("second", second)
            .set_entry("first").add_edge("first", "second", when="next")
            .compile()
        )
        result = Runtime().run(graph, {"value": "original"})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(observed, [("original", 0), ("first", 1)])

    def test_non_string_update_key_is_rejected_without_state_change(self) -> None:
        graph = Graph().add_node(
            "bad", lambda ctx: NodeResult.success({1: "invalid"})
        ).set_entry("bad").compile()
        result = Runtime().run(graph, {"input": "original"})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "INVALID_STATE_UPDATE")
        self.assertEqual(result.state, {"input": "original"})

    def test_nested_non_string_update_key_is_rejected(self) -> None:
        graph = Graph().add_node(
            "bad", lambda ctx: NodeResult.success({"nested": {1: "invalid"}})
        ).set_entry("bad").compile()
        result = Runtime().run(graph, {})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "INVALID_STATE_UPDATE")


if __name__ == "__main__":
    unittest.main()

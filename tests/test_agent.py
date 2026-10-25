"""Composable Agent shell contract tests."""

import tempfile
import threading
import unittest
from time import monotonic, sleep
from pathlib import Path

from agent_core import (
    AgentBuilder,
    AgentSpec,
    InMemoryMemory,
    NodeResult,
    RunStatus,
    RunStore,
    Runtime,
    Tool,
    ToolRegistry,
)


class AgentShellTests(unittest.TestCase):
    def test_agent_runs_graph_and_wraps_result(self) -> None:
        agent = (
            AgentBuilder("text-agent", instructions="处理用户文本")
            .add_node("prepare", lambda ctx: NodeResult.success({"text": ctx.input["text"]}))
            .add_node("finish", lambda ctx: NodeResult.success({"done": True}))
            .set_entry("prepare")
            .add_edge("prepare", "finish")
            .build()
        )
        result = agent.run({"text": "hello"}, run_id="agent-happy")
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.run_id, "agent-happy")
        self.assertEqual(result.state["text"], "hello")
        self.assertEqual(result.state["done"], True)
        self.assertEqual(len(result.trace), 9)
        self.assertEqual(agent.spec.name, "text-agent")
        self.assertEqual(agent.spec.instructions, "处理用户文本")

    def test_agent_result_exposes_underlying_run(self) -> None:
        agent = (
            AgentBuilder("agent")
            .add_node("only", lambda ctx: NodeResult.success({"ok": True}))
            .set_entry("only")
            .build()
        )
        result = agent.run({})
        self.assertEqual(result.run_id, result.run.run_id)
        self.assertEqual(result.status, result.run.status)
        self.assertEqual(result.state, result.run.state)
        self.assertEqual(result.trace, result.run.trace)
        self.assertEqual(result.run.lifecycle_events[0].event_type, "run.created")

    def test_agent_describe_exposes_graph_topology(self) -> None:
        agent = (
            AgentBuilder("agent")
            .add_node("first", lambda ctx: NodeResult.success(route="next"))
            .add_node("second", lambda ctx: NodeResult.success(route="done"))
            .add_node("third", lambda ctx: NodeResult.success())
            .set_entry("first")
            .add_edge("first", "second", when="next")
            .add_edge("second", "third", when="done")
            .build()
        )
        description = agent.describe()
        self.assertEqual(description["entry"], "first")
        self.assertEqual(description["nodes"], ["first", "second", "third"])
        self.assertEqual(
            description["edges"],
            [
                {"source": "first", "target": "second", "when": "next"},
                {"source": "second", "target": "third", "when": "done"},
            ],
        )

    def test_agent_requires_mapping_input(self) -> None:
        agent = (
            AgentBuilder("agent")
            .add_node("only", lambda ctx: NodeResult.success())
            .set_entry("only")
            .build()
        )
        with self.assertRaises(TypeError):
            agent.run(["not", "a", "mapping"])

    def test_agent_build_requires_entry_node(self) -> None:
        builder = AgentBuilder("agent").add_node("only", lambda ctx: NodeResult.success())
        with self.assertRaises(ValueError):
            builder.build()

    def test_agent_builder_rejects_non_callable_node(self) -> None:
        builder = AgentBuilder("agent")
        with self.assertRaises(TypeError):
            builder.add_node("only", "not-callable")

    def test_agent_spec_rejects_invalid_fields(self) -> None:
        with self.assertRaises(ValueError):
            AgentSpec("   ")
        with self.assertRaises(TypeError):
            AgentSpec("agent", instructions=123)
        with self.assertRaises(TypeError):
            AgentSpec("agent", metadata={1: "not-a-string-key"})

    def test_agent_spec_metadata_is_snapshot_isolated(self) -> None:
        metadata = {"tier": "gold", "tags": ["a"]}
        spec = AgentSpec("agent", metadata=metadata)
        metadata["tags"].append("b")
        metadata["tier"] = "silver"
        self.assertEqual(spec.metadata["tier"], "gold")
        self.assertEqual(spec.metadata["tags"], ["a"])

    def test_agent_node_receives_injected_dependencies(self) -> None:
        seen: dict[str, object] = {}

        class EchoModel:
            def complete(self, messages, *, tools=None, context=None):
                return {"reply": "ok"}

        model = EchoModel()
        memory = InMemoryMemory({"greeting": "你好"})
        memory.write("counter", 3)
        registry = ToolRegistry()
        registry.register(Tool("echo", lambda args: {"echo": args["value"]}))

        def node(ctx):
            seen["instructions"] = ctx.instructions
            seen["metadata"] = dict(ctx.agent_metadata)
            seen["model_reply"] = ctx.model.complete([])["reply"]
            seen["tool"] = ctx.tools.invoke("echo", {"value": "hi"}).output["echo"]
            seen["memory_read"] = ctx.memory.read("greeting")
            seen["memory_count"] = ctx.memory.read("counter")
            seen["cancel_requested"] = ctx.cancel_requested
            return NodeResult.success({"observed": True})

        agent = (
            AgentBuilder(
                "agent",
                instructions="注入检查",
                metadata={"tenant": "t1"},
                model=model,
                tools=registry,
                memory=memory,
            )
            .add_node("probe", node)
            .set_entry("probe")
            .build()
        )
        result = agent.run({})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(seen["instructions"], "注入检查")
        self.assertEqual(seen["metadata"], {"tenant": "t1"})
        self.assertEqual(seen["model_reply"], "ok")
        self.assertEqual(seen["tool"], "hi")
        self.assertEqual(seen["memory_read"], "你好")
        self.assertEqual(seen["memory_count"], 3)
        self.assertIs(seen["cancel_requested"], False)

    def test_agent_blocked_node_stops_run_with_reason(self) -> None:
        downstream_calls: list[str] = []
        agent = (
            AgentBuilder("agent")
            .add_node("gate", lambda ctx: NodeResult.blocked("EMPTY_TEXT"))
            .add_node(
                "downstream",
                lambda ctx: (downstream_calls.append("hit") or NodeResult.success()),
            )
            .set_entry("gate")
            .add_edge("gate", "downstream")
            .build()
        )
        result = agent.run({})
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "EMPTY_TEXT")
        self.assertEqual(downstream_calls, [])

    def test_agent_uses_injected_runtime_and_persists_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs")
            runtime = Runtime(store=store)
            agent = (
                AgentBuilder("agent")
                .add_node("only", lambda ctx: NodeResult.success({"done": True}))
                .set_entry("only")
                .build(runtime=runtime)
            )
            result = agent.run({}, run_id="agent-persisted")
            self.assertEqual(result.status, RunStatus.COMPLETED)
            self.assertIn("agent-persisted", store.list_runs())
            self.assertEqual(runtime.get_status("agent-persisted"), RunStatus.COMPLETED)
            stored = store.load("agent-persisted")
            self.assertIsNotNone(stored)
            self.assertEqual(stored.state["done"], True)

    def test_agent_honors_cooperative_cancellation(self) -> None:
        runtime = Runtime()
        started = threading.Event()

        def slow_node(ctx):
            started.set()
            deadline = monotonic() + 5.0
            while monotonic() < deadline and not ctx.cancel_requested:
                sleep(0.01)
            return NodeResult.success({"waited": True})

        agent = (
            AgentBuilder("agent")
            .add_node("slow", slow_node)
            .add_node("after", lambda ctx: NodeResult.success({"skipped": True}))
            .set_entry("slow")
            .add_edge("slow", "after")
            .build(runtime=runtime)
        )
        outcome: dict[str, object] = {}

        def execute() -> None:
            outcome["result"] = agent.run({}, run_id="agent-cancel-it")

        worker = threading.Thread(target=execute)
        worker.start()
        self.assertTrue(started.wait(timeout=5.0))
        accepted = False
        deadline = monotonic() + 5.0
        while monotonic() < deadline:
            if runtime.cancel("agent-cancel-it"):
                accepted = True
                break
            sleep(0.005)
        self.assertTrue(accepted)
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())
        result = outcome["result"]
        self.assertEqual(result.status, RunStatus.CANCELED)
        self.assertEqual(result.reason_code, "CANCEL_REQUESTED")
        self.assertEqual(result.run.trace[-1].event_type, "run.canceled")


class InMemoryMemoryTests(unittest.TestCase):
    def test_initial_values_are_snapshotted(self) -> None:
        initial = {"nested": {"count": 1}, "items": ["a"]}
        memory = InMemoryMemory(initial)
        initial["nested"]["count"] = 99
        initial["items"].append("b")
        self.assertEqual(memory.read("nested"), {"count": 1})
        self.assertEqual(memory.read("items"), ["a"])

    def test_read_returns_isolated_copies(self) -> None:
        memory = InMemoryMemory({"nested": {"count": 1}})
        value = memory.read("nested")
        value["count"] = 42
        self.assertEqual(memory.read("nested"), {"count": 1})

    def test_read_returns_default_for_missing_key(self) -> None:
        memory = InMemoryMemory()
        self.assertIsNone(memory.read("missing"))
        self.assertEqual(memory.read("missing", "fallback"), "fallback")

    def test_write_snapshots_value(self) -> None:
        memory = InMemoryMemory()
        value = {"count": 1}
        memory.write("nested", value)
        value["count"] = 99
        self.assertEqual(memory.read("nested"), {"count": 1})

    def test_rejects_invalid_keys(self) -> None:
        memory = InMemoryMemory()
        for key in ("", "   ", None, 7):
            with self.assertRaises(ValueError):
                memory.read(key)
            with self.assertRaises(ValueError):
                memory.write(key, 1)

    def test_rejects_non_mapping_initial(self) -> None:
        with self.assertRaises(TypeError):
            InMemoryMemory(["not", "a", "mapping"])


if __name__ == "__main__":
    unittest.main()

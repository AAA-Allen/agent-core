"""v0.1 Graph Runtime contract tests."""

import unittest

from agent_core import Graph, NodeResult, NodeStatus, RunStatus, Runtime


class GraphRuntimeTests(unittest.TestCase):
    def test_node_result_rejects_invalid_status_combinations(self) -> None:
        with self.assertRaises(TypeError):
            NodeResult(status="SUCCEEDED")
        with self.assertRaises(ValueError):
            NodeResult(NodeStatus.SUCCEEDED, reason_code="unexpected")
        with self.assertRaises(ValueError):
            NodeResult(NodeStatus.BLOCKED)
        with self.assertRaises(ValueError):
            NodeResult(NodeStatus.FAILED, route="next", reason_code="failed")

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

    def test_non_string_update_key_is_rejected_without_state_change(self) -> None:
        graph = Graph().add_node(
            "bad", lambda ctx: NodeResult.success({1: "invalid"})
        ).set_entry("bad").compile()
        result = Runtime().run(graph, {"input": "original"})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "INVALID_STATE_UPDATE")
        self.assertEqual(result.state, {"input": "original"})


if __name__ == "__main__":
    unittest.main()

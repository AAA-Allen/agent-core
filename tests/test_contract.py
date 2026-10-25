"""Runtime contract tests calibrated from agent_runtime_document_pack.

The pack's ``tests/test_runtime_contract.py`` defined the target v0.1 API
before the implementation was reviewed. This module adapts those contracts to
the real API one-for-one (same behaviors, same names where possible):

- target ``NodeResult(status="OK")`` is the implemented
  ``NodeResult.success(...)`` (three-state NodeStatus enum);
- everything else keeps the pack's semantics: ordered execution, conditional
  routing, fail-closed BLOCKED/FAILED, bounded loops, explicit simulation
  marking, and run-scoped ordered trace.
"""

import unittest

from agent_core import Graph, NodeResult, RunStatus, Runtime


def ok_node(route=None, updates=None, output=None, simulated=False):
    def _run(ctx):
        return NodeResult.success(updates or {}, route=route, output=output, simulated=simulated)
    return _run


def blocked_node(reason_code="REVIEW_REQUIRED"):
    def _run(ctx):
        return NodeResult.blocked(reason_code)
    return _run


def failed_node(reason_code="NODE_EXECUTION_ERROR"):
    def _run(ctx):
        return NodeResult.failed(reason_code)
    return _run


class RuntimeContractTests(unittest.TestCase):
    def test_linear_graph_executes_in_order(self) -> None:
        calls = []

        def a(ctx):
            calls.append("a")
            return NodeResult.success({"a": 1})

        def b(ctx):
            calls.append("b")
            return NodeResult.success({"b": 2})

        graph = (
            Graph()
            .add_node("a", a)
            .add_node("b", b)
            .set_entry("a")
            .add_edge("a", "b")
            .compile()
        )
        result = Runtime().run(graph, input={})

        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state["a"], 1)
        self.assertEqual(result.state["b"], 2)

    def test_conditional_route_runs_only_matching_branch(self) -> None:
        calls = []

        def choose(ctx):
            return NodeResult.success({}, route="yes")

        def yes(ctx):
            calls.append("yes")
            return NodeResult.success({"chosen": "yes"})

        def no(ctx):
            calls.append("no")
            return NodeResult.success({"chosen": "no"})

        graph = (
            Graph()
            .add_node("choose", choose)
            .add_node("yes", yes)
            .add_node("no", no)
            .set_entry("choose")
            .add_edge("choose", "yes", when="yes")
            .add_edge("choose", "no", when="no")
            .compile()
        )
        result = Runtime().run(graph, input={})

        self.assertEqual(calls, ["yes"])
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state["chosen"], "yes")

    def test_blocked_node_stops_downstream(self) -> None:
        calls = []

        def downstream(ctx):
            calls.append("downstream")
            return NodeResult.success({})

        graph = (
            Graph()
            .add_node("gate", blocked_node())
            .add_node("downstream", downstream)
            .set_entry("gate")
            .add_edge("gate", "downstream")
            .compile()
        )
        result = Runtime().run(graph, input={})

        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "REVIEW_REQUIRED")
        self.assertEqual(calls, [])

    def test_failed_node_stops_downstream(self) -> None:
        calls = []

        def downstream(ctx):
            calls.append("downstream")
            return NodeResult.success({})

        graph = (
            Graph()
            .add_node("work", failed_node())
            .add_node("downstream", downstream)
            .set_entry("work")
            .add_edge("work", "downstream")
            .compile()
        )
        result = Runtime().run(graph, input={})

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "NODE_EXECUTION_ERROR")
        self.assertEqual(calls, [])

    def test_node_exception_stops_downstream_without_leaking_message(self) -> None:
        calls = []

        def explode(ctx):
            raise RuntimeError("test explosion")

        def downstream(ctx):
            calls.append("downstream")
            return NodeResult.success({})

        graph = (
            Graph()
            .add_node("explode", explode)
            .add_node("downstream", downstream)
            .set_entry("explode")
            .add_edge("explode", "downstream")
            .compile()
        )
        result = Runtime().run(graph, input={})

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(calls, [])
        serialized = repr([event.__dict__ for event in result.trace])
        self.assertNotIn("test explosion", serialized)
        self.assertEqual(
            [event.error_type for event in result.trace if event.error_type],
            ["RuntimeError"],
        )

    def test_invalid_edge_target_is_rejected_at_compile(self) -> None:
        graph = (
            Graph()
            .add_node("a", ok_node())
            .set_entry("a")
            .add_edge("a", "missing")
        )
        with self.assertRaises(ValueError):
            graph.compile()

    def test_duplicate_node_id_is_not_silently_overwritten(self) -> None:
        graph = Graph().add_node("same", ok_node())
        with self.assertRaises(ValueError):
            graph.add_node("same", ok_node())

    def test_loop_is_bounded_by_max_steps(self) -> None:
        def loop(ctx):
            return NodeResult.success({}, route="again")

        graph = (
            Graph()
            .add_node("loop", loop)
            .set_entry("loop")
            .add_edge("loop", "loop", when="again")
            .compile()
        )
        result = Runtime(max_steps=3).run(graph, input={})

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "MAX_STEPS_EXCEEDED")

    def test_simulated_result_remains_explicit_in_final_result(self) -> None:
        graph = (
            Graph()
            .add_node("demo", ok_node(simulated=True))
            .set_entry("demo")
            .compile()
        )
        result = Runtime().run(graph, input={})

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertIs(result.simulated, True)

    def test_trace_events_are_linked_to_run_and_ordered(self) -> None:
        graph = (
            Graph()
            .add_node("one", ok_node())
            .set_entry("one")
            .compile()
        )
        result = Runtime().run(graph, input={})

        self.assertTrue(result.run_id)
        self.assertTrue(result.trace)
        self.assertTrue(all(event.run_id == result.run_id for event in result.trace))
        sequences = [event.sequence for event in result.trace]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(sequences), len(set(sequences)))


if __name__ == "__main__":
    unittest.main()

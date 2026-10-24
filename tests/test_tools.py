"""Framework-level tests for agent_core.tools — no business vocabulary here."""

import unittest

from agent_core import (
    RunStatus,
    Tool,
    ToolRegistry,
    graph_tool_node,
    WorkflowRunner,
    tool_step,
)


class ToolValidationTests(unittest.TestCase):
    def test_blank_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Tool("  ", lambda args: {})

    def test_invoke_must_be_callable(self) -> None:
        with self.assertRaises(TypeError):
            Tool("t", "not-callable")

    def test_capabilities_must_be_frozenset_of_nonempty_strings(self) -> None:
        with self.assertRaises(ValueError):
            Tool("t", lambda args: {}, required_capabilities={"a"})
        with self.assertRaises(ValueError):
            Tool("t", lambda args: {}, required_capabilities=frozenset({" "}))


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register(Tool("echo", lambda args: {"text": args["text"]}))

    def test_duplicate_registration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.register(Tool("echo", lambda args: {}))

    def test_concurrent_registration_keeps_one_definition(self) -> None:
        import threading

        errors: list[Exception] = []

        def register() -> None:
            try:
                self.registry.register(Tool("race", lambda args: {}))
            except Exception as exc:
                errors.append(exc)

        workers = [threading.Thread(target=register) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(1)
        self.assertEqual(sum(isinstance(error, ValueError) for error in errors), 3)
        self.assertEqual(self.registry.invoke("race", {}).status.value, "COMPLETED")

    def test_non_tool_registration_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            self.registry.register("not-a-tool")

    def test_unknown_tool_is_blocked(self) -> None:
        result = self.registry.invoke("missing", {})
        self.assertEqual(result.status.value, "BLOCKED")
        self.assertEqual(result.reason_code, "TOOL_NOT_FOUND")

    def test_blank_tool_name_is_blocked(self) -> None:
        result = self.registry.invoke("  ", {})
        self.assertEqual(result.reason_code, "INVALID_TOOL_NAME")

    def test_arguments_must_be_mapping(self) -> None:
        result = self.registry.invoke("echo", ["not", "a", "mapping"])
        self.assertEqual(result.reason_code, "INVALID_ARGUMENTS")

    def test_non_finite_arguments_are_blocked(self) -> None:
        result = self.registry.invoke("echo", {"text": float("nan")})
        self.assertEqual(result.reason_code, "INVALID_ARGUMENTS")

    def test_tool_exception_becomes_failed_result(self) -> None:
        self.registry.register(Tool("boom", lambda args: 1 / 0))
        result = self.registry.invoke("boom", {})
        self.assertEqual(result.status.value, "FAILED")
        self.assertEqual(result.reason_code, "TOOL_EXCEPTION")
        self.assertEqual(result.error_type, "ZeroDivisionError")

    def test_non_mapping_output_is_failed(self) -> None:
        self.registry.register(Tool("bad", lambda args: [1, 2]))
        result = self.registry.invoke("bad", {})
        self.assertEqual(result.reason_code, "INVALID_TOOL_OUTPUT")

    def test_non_finite_output_is_failed(self) -> None:
        self.registry.register(Tool("bad", lambda args: {"x": float("inf")}))
        result = self.registry.invoke("bad", {})
        self.assertEqual(result.reason_code, "INVALID_TOOL_OUTPUT")

    def test_successful_invocation(self) -> None:
        result = self.registry.invoke("echo", {"text": "hello"})
        self.assertEqual(result.status.value, "COMPLETED")
        self.assertEqual(result.output, {"text": "hello"})

    def test_tool_result_rejects_invalid_direct_construction(self) -> None:
        from agent_core import ToolResult, ToolStatus

        with self.assertRaises(TypeError):
            ToolResult("echo", "COMPLETED")
        with self.assertRaises(ValueError):
            ToolResult("echo", ToolStatus.FAILED, reason_code=" ")
        with self.assertRaises(ValueError):
            ToolResult("echo", ToolStatus.COMPLETED, reason_code="UNEXPECTED")
        with self.assertRaises(ValueError):
            ToolResult("echo", ToolStatus.BLOCKED)
        with self.assertRaises(ValueError):
            ToolResult("echo", ToolStatus.BLOCKED, reason_code="DENIED", error_type="ValueError")

    def test_tool_result_detaches_nested_output(self) -> None:
        output = {"nested": {"value": 1}}
        result = self.registry.invoke("echo", {"text": "hello"})
        self.registry.register(Tool("nested", lambda args: output))
        nested = self.registry.invoke("nested", {})
        output["nested"]["value"] = 9
        self.assertEqual(nested.output["nested"]["value"], 1)

    def test_capability_check(self) -> None:
        self.registry.register(
            Tool("guarded", lambda args: {"ok": True}, required_capabilities=frozenset({"read"}))
        )
        denied = self.registry.invoke("guarded", {}, granted_capabilities=frozenset())
        self.assertEqual(denied.reason_code, "CAPABILITY_DENIED")
        granted = self.registry.invoke(
            "guarded", {}, granted_capabilities=frozenset({"read"})
        )
        self.assertEqual(granted.status.value, "COMPLETED")

    def test_granted_capabilities_must_be_frozenset(self) -> None:
        result = self.registry.invoke("echo", {}, granted_capabilities={"read"})
        self.assertEqual(result.reason_code, "CAPABILITY_DENIED")


class ToolStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register(Tool("echo", lambda args: {"text": args["text"]}))

    def test_completed_tool_maps_to_completed_step(self) -> None:
        workflow_step = tool_step(
            "call", "echo", self.registry, lambda context: {"text": context.input["text"]}
        )
        result = WorkflowRunner([workflow_step]).run({"text": "hi"}, run_id="t1")
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.outputs["call"], {"text": "hi"})

    def test_blocked_tool_stops_workflow_with_reason_code(self) -> None:
        workflow_step = tool_step(
            "call", "missing", self.registry, lambda context: {}
        )
        result = WorkflowRunner([workflow_step]).run({}, run_id="t2")
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "TOOL_NOT_FOUND")

    def test_failed_tool_stops_workflow(self) -> None:
        self.registry.register(Tool("boom", lambda args: 1 / 0))
        workflow_step = tool_step("call", "boom", self.registry, lambda context: {})
        result = WorkflowRunner([workflow_step]).run({}, run_id="t3")
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "TOOL_EXCEPTION")

    def test_granted_capabilities_are_fixed_at_build_time(self) -> None:
        self.registry.register(
            Tool("guarded", lambda args: {"ok": True}, required_capabilities=frozenset({"read"}))
        )
        workflow_step = tool_step(
            "call", "guarded", self.registry, lambda context: {}
        )
        result = WorkflowRunner([workflow_step]).run({}, run_id="t4")
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "CAPABILITY_DENIED")


class GraphToolNodeTests(unittest.TestCase):
    def test_invalid_builder_arguments_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            graph_tool_node("echo", "not-a-registry", lambda ctx: {})
        with self.assertRaises(TypeError):
            graph_tool_node("echo", ToolRegistry(), {})

    def test_capabilities_are_enforced_by_graph_adapter(self) -> None:
        registry = ToolRegistry()
        registry.register(
            Tool("guarded", lambda args: {"ok": True}, required_capabilities=frozenset({"read"}))
        )
        denied = graph_tool_node("guarded", registry, lambda ctx: {})
        self.assertEqual(denied(None).reason_code, "CAPABILITY_DENIED")
        granted = graph_tool_node(
            "guarded", registry, lambda ctx: {}, granted_capabilities=frozenset({"read"})
        )
        self.assertEqual(granted(None).status.value, "SUCCEEDED")


if __name__ == "__main__":
    unittest.main()

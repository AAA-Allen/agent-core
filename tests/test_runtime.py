"""Framework-level tests for agent_core.runtime — no business vocabulary here."""

import unittest

from agent_core import (
    RunContext,
    RunResult,
    RunStatus,
    Step,
    StepOutcome,
    StepStatus,
    TraceEvent,
    WorkflowRunner,
)


def _ok_step(name: str, output: dict | None = None) -> Step:
    return Step(name, lambda context: StepOutcome(StepStatus.COMPLETED, output or {"done": True}))


class StepOutcomeValidationTests(unittest.TestCase):
    def test_completed_without_reason_code_is_valid(self) -> None:
        outcome = StepOutcome(StepStatus.COMPLETED)
        self.assertIsNone(outcome.reason_code)

    def test_blocked_requires_reason_code(self) -> None:
        with self.assertRaises(ValueError):
            StepOutcome(StepStatus.BLOCKED)

    def test_failed_requires_reason_code(self) -> None:
        with self.assertRaises(ValueError):
            StepOutcome(StepStatus.FAILED)

    def test_blank_reason_code_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            StepOutcome(StepStatus.BLOCKED, reason_code="   ")

    def test_status_must_be_step_status(self) -> None:
        with self.assertRaises(TypeError):
            StepOutcome("COMPLETED")

    def test_output_must_be_mapping(self) -> None:
        with self.assertRaises(TypeError):
            StepOutcome(StepStatus.COMPLETED, output=[1, 2])


class StepValidationTests(unittest.TestCase):
    def test_blank_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Step("  ", lambda context: StepOutcome(StepStatus.COMPLETED))

    def test_execute_must_be_callable(self) -> None:
        with self.assertRaises(TypeError):
            Step("a", "not-callable")


class RunContextSnapshotTests(unittest.TestCase):
    def test_input_reads_return_detached_copies(self) -> None:
        context = RunContext(run_id="r1", _input={"sample_id": "001"})
        snapshot = context.input
        snapshot["sample_id"] = "mutated"
        self.assertEqual(context.input["sample_id"], "001")

    def test_output_of_is_a_detached_snapshot(self) -> None:
        context = RunContext(run_id="r1", _input={})
        context._outputs["a"] = {"value": 1}
        snapshot = context.output_of("a")
        snapshot["value"] = 999
        self.assertEqual(context.output_of("a")["value"], 1)

    def test_non_finite_input_is_rejected_on_read(self) -> None:
        context = RunContext(run_id="r1", _input={"x": float("nan")})
        with self.assertRaises(ValueError):
            context.input

    def test_non_serializable_input_is_rejected_on_read(self) -> None:
        context = RunContext(run_id="r1", _input={"x": {1, 2}})
        with self.assertRaises(TypeError):
            context.input


class WorkflowRunnerConstructionTests(unittest.TestCase):
    def test_empty_steps_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkflowRunner([])

    def test_non_step_entries_are_rejected(self) -> None:
        with self.assertRaises(TypeError):
            WorkflowRunner([_ok_step("a"), "not-a-step"])

    def test_duplicate_step_names_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkflowRunner([_ok_step("a"), _ok_step("a")])


class WorkflowRunnerExecutionTests(unittest.TestCase):
    def test_steps_run_in_order_and_outputs_accumulate(self) -> None:
        order: list[str] = []

        def make(name: str) -> Step:
            def execute(context: RunContext) -> StepOutcome:
                order.append(name)
                return StepOutcome(StepStatus.COMPLETED, {"ran": name})
            return Step(name, execute)

        workflow = WorkflowRunner([make("a"), make("b"), make("c")])
        result = workflow.run({"x": 1}, run_id="ordered")
        self.assertEqual(order, ["a", "b", "c"])
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.outputs["b"], {"ran": "b"})

    def test_blocked_stops_later_steps(self) -> None:
        ran: list[str] = []

        def blocked(context: RunContext) -> StepOutcome:
            ran.append("a")
            return StepOutcome(StepStatus.BLOCKED, reason_code="STOP")

        def never(context: RunContext) -> StepOutcome:
            ran.append("b")
            return StepOutcome(StepStatus.COMPLETED)

        workflow = WorkflowRunner([Step("a", blocked), Step("b", never)])
        result = workflow.run({"x": 1}, run_id="blocked-run")
        self.assertEqual(ran, ["a"])
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.stopped_at, "a")
        self.assertEqual(result.reason_code, "STOP")

    def test_step_exception_becomes_failed_run(self) -> None:
        def boom(context: RunContext) -> StepOutcome:
            raise RuntimeError("kaboom")

        workflow = WorkflowRunner([Step("a", boom)])
        result = workflow.run({"x": 1}, run_id="boom-run")
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "STEP_EXCEPTION")
        self.assertEqual(result.trace[0].error_type, "RuntimeError")

    def test_blocked_step_output_is_not_committed(self) -> None:
        workflow = WorkflowRunner([
            Step("stop", lambda ctx: StepOutcome(
                StepStatus.BLOCKED, {"secret": "uncommitted"}, "STOP"
            )),
        ])
        result = workflow.run({})
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.outputs, {})

    def test_failed_step_output_is_not_committed(self) -> None:
        workflow = WorkflowRunner([
            Step("stop", lambda ctx: StepOutcome(
                StepStatus.FAILED, {"secret": "uncommitted"}, "BROKEN"
            )),
        ])
        result = workflow.run({})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.outputs, {})

    def test_non_outcome_return_becomes_failed_run(self) -> None:
        workflow = WorkflowRunner([Step("a", lambda context: {"not": "an outcome"})])
        result = workflow.run({"x": 1}, run_id="bad-return")
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.trace[0].error_type, "TypeError")

    def test_non_finite_step_output_becomes_failed_run(self) -> None:
        workflow = WorkflowRunner([
            Step("a", lambda context: StepOutcome(StepStatus.COMPLETED, {"x": float("inf")})),
        ])
        result = workflow.run({"x": 1}, run_id="nan-output")
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.trace[0].error_type, "ValueError")

    def test_trace_event_fields(self) -> None:
        workflow = WorkflowRunner([_ok_step("a")])
        result = workflow.run({"x": 1}, run_id="trace-run")
        event = result.trace[0]
        self.assertIsInstance(event, TraceEvent)
        self.assertEqual(event.run_id, "trace-run")
        self.assertEqual(event.step, "a")
        self.assertEqual(event.status, "COMPLETED")
        self.assertTrue(event.started_at.endswith("+00:00"))
        self.assertGreaterEqual(event.duration_ms, 0.0)
        self.assertIsNone(event.reason_code)
        self.assertIsNone(event.error_type)

    def test_run_id_is_generated_when_absent(self) -> None:
        workflow = WorkflowRunner([_ok_step("a")])
        result = workflow.run({"x": 1})
        self.assertEqual(len(result.run_id), 32)
        self.assertEqual(result.trace[0].run_id, result.run_id)

    def test_input_must_be_mapping(self) -> None:
        workflow = WorkflowRunner([_ok_step("a")])
        with self.assertRaises(TypeError):
            workflow.run([1, 2, 3])

    def test_run_id_length_is_bounded(self) -> None:
        workflow = WorkflowRunner([_ok_step("a")])
        with self.assertRaises(ValueError):
            workflow.run({"x": 1}, run_id="x" * 129)
        with self.assertRaises(ValueError):
            workflow.run({"x": 1}, run_id="   ")

    def test_run_id_cannot_escape_persistence_path(self) -> None:
        workflow = WorkflowRunner([_ok_step("a")])
        for run_id in ("../escape", "..\\escape", "nested/id", "C:"):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                workflow.run({"x": 1}, run_id=run_id)


if __name__ == "__main__":
    unittest.main()

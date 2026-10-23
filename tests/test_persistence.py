import tempfile
import unittest
from pathlib import Path

from agent_core import (
    Graph,
    NodeResult,
    RunContext,
    RunResult,
    RunStatus,
    RunStore,
    Step,
    StepOutcome,
    StepStatus,
    TraceEvent,
    WorkflowRunner,
)


def _sample_result(run_id: str = "run-1") -> RunResult:
    return RunResult(
        run_id=run_id,
        status=RunStatus.COMPLETED,
        outputs={"step_a": {"value": 1}},
        trace=(
            TraceEvent(run_id, "step_a", "COMPLETED", "2026-10-19T00:00:00+00:00", 1.5, None, None),
        ),
        stopped_at=None,
        reason_code=None,
    )


class RunStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_save_load_roundtrip(self) -> None:
        result = _sample_result()
        path = self.store.save(result)
        self.assertTrue(path.is_file())
        self.assertEqual(self.store.load("run-1"), result)

    def test_saved_file_is_valid_json(self) -> None:
        path = self.store.save(_sample_result())
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["status"], "COMPLETED")
        self.assertEqual(payload["trace"][0]["step"], "step_a")

    def test_load_missing_returns_none(self) -> None:
        self.assertIsNone(self.store.load("never-saved"))

    def test_path_like_run_ids_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.load("../outside")
        with self.assertRaises(ValueError):
            self.store.save(_sample_result("nested/name"))

    def test_corrupt_json_is_reported_as_invalid_run_record(self) -> None:
        path = self.store.directory / "broken.json"
        self.store.directory.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid run record"):
            self.store.load("broken")

    def test_missing_required_record_field_is_reported_as_invalid_run_record(self) -> None:
        path = self.store.directory / "missing.json"
        self.store.directory.mkdir(parents=True, exist_ok=True)
        path.write_text('{"run_id": "missing", "status": "COMPLETED"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid run record"):
            self.store.load("missing")

    def test_record_run_id_must_match_filename(self) -> None:
        path = self.store.directory / "requested.json"
        self.store.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"run_id": "different", "status": "COMPLETED", "outputs": {}, "trace": []}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "invalid run record"):
            self.store.load("requested")

    def test_list_runs_sorted(self) -> None:
        self.store.save(_sample_result("run-b"))
        self.store.save(_sample_result("run-a"))
        self.assertEqual(self.store.list_runs(), ["run-a", "run-b"])

    def test_save_overwrites_same_run_id(self) -> None:
        self.store.save(_sample_result("dup"))
        blocked = RunResult(
            run_id="dup",
            status=RunStatus.BLOCKED,
            outputs={},
            trace=(),
            stopped_at="step_a",
            reason_code="SOME_REASON",
        )
        self.store.save(blocked)
        self.assertEqual(self.store.load("dup"), blocked)
        self.assertEqual(self.store.list_runs(), ["dup"])

    def test_blocked_and_failed_results_roundtrip(self) -> None:
        for status, stopped, reason in (
            (RunStatus.BLOCKED, "step_a", "CAPABILITY_DENIED"),
            (RunStatus.FAILED, "step_b", "STEP_EXCEPTION"),
        ):
            result = RunResult(
                run_id=f"run-{status.value}",
                status=status,
                outputs={"step_a": {"value": 1}},
                trace=(
                    TraceEvent(
                        f"run-{status.value}", "step_a", status.value,
                        "2026-10-19T00:00:00+00:00", 2.0, reason, "RuntimeError",
                    ),
                ),
                stopped_at=stopped,
                reason_code=reason,
            )
            self.store.save(result)
            self.assertEqual(self.store.load(f"run-{status.value}"), result)


class WorkflowRunnerPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_completed_run_is_persisted(self) -> None:
        workflow = WorkflowRunner(
            [Step("a", lambda ctx: StepOutcome(StepStatus.COMPLETED, {"v": 1}))],
            store=self.store,
        )
        result = workflow.run({"x": 1}, run_id="fixed-id")
        self.assertEqual(self.store.load("fixed-id"), result)

    def test_blocked_run_is_persisted(self) -> None:
        def blocked(context: RunContext) -> StepOutcome:
            return StepOutcome(StepStatus.BLOCKED, reason_code="STOP_HERE")

        workflow = WorkflowRunner([Step("a", blocked)], store=self.store)
        result = workflow.run({"x": 1}, run_id="blocked-id")
        self.assertEqual(result.status, RunStatus.BLOCKED)
        loaded = self.store.load("blocked-id")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.reason_code, "STOP_HERE")

    def test_step_exception_run_is_persisted(self) -> None:
        def boom(context: RunContext) -> StepOutcome:
            raise RuntimeError("kaboom")

        workflow = WorkflowRunner([Step("a", boom)], store=self.store)
        result = workflow.run({"x": 1}, run_id="boom-id")
        self.assertEqual(result.status, RunStatus.FAILED)
        loaded = self.store.load("boom-id")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.reason_code, "STEP_EXCEPTION")
        self.assertEqual(loaded.trace[0].error_type, "RuntimeError")

    def test_without_store_nothing_is_written(self) -> None:
        workflow = WorkflowRunner(
            [Step("a", lambda ctx: StepOutcome(StepStatus.COMPLETED, {"v": 1}))]
        )
        workflow.run({"x": 1}, run_id="no-store")
        self.assertFalse((Path(self._tmp.name) / "no-store.json").exists())
        self.assertEqual(self.store.list_runs(), [])


class GraphRuntimePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_graph_result_roundtrips_state_trace_and_simulation_flag(self) -> None:
        from agent_core import Runtime

        graph = Graph().add_node(
            "demo", lambda ctx: NodeResult.success({"answer": "preview"}, simulated=True)
        ).set_entry("demo").compile()
        result = Runtime().run(graph, {}, run_id="graph-preview")

        path = self.store.save(result)
        loaded = self.store.load("graph-preview")

        self.assertTrue(path.is_file())
        self.assertEqual(loaded, result)
        self.assertEqual(loaded.state["answer"], "preview")
        self.assertTrue(loaded.simulated)
        self.assertEqual(
            [event.event_type for event in loaded.trace],
            [event.event_type for event in result.trace],
        )
        self.assertEqual(loaded.lifecycle_events, result.lifecycle_events)

    def test_graph_lifecycle_events_roundtrip_for_canceled_run(self) -> None:
        from agent_core import Runtime

        runtime = Runtime()

        def stop(ctx):
            runtime.cancel(ctx.run_id)
            return NodeResult.success({}, route="next")

        graph = (
            Graph().add_node("stop", stop)
            .add_node("never", lambda ctx: NodeResult.success())
            .set_entry("stop").add_edge("stop", "never", when="next").compile()
        )
        result = runtime.run(graph, {}, run_id="persisted-cancel")
        self.assertEqual(result.status, RunStatus.CANCELED)
        self.store.save(result)
        loaded = self.store.load("persisted-cancel")
        self.assertEqual(
            [event.event_type for event in loaded.lifecycle_events],
            ["run.created", "run.running", "run.canceled"],
        )
        self.assertEqual(loaded.lifecycle_events[-1].to_status.value, "CANCELED")

    def test_run_store_rejects_mismatched_lifecycle_event(self) -> None:
        path = Path(self._tmp.name) / "bad-lifecycle.json"
        path.write_text(
            '{"run_id":"bad-lifecycle","status":"COMPLETED","outputs":{},"trace":[],'
            '"lifecycle_events":[{"run_id":"other","sequence":1,"event_id":"e",'
            '"event_type":"run.running","from_status":"PENDING","to_status":"RUNNING",'
            '"occurred_at":"2026-10-24T00:00:00+00:00"}]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "invalid run record"):
            self.store.load("bad-lifecycle")


if __name__ == "__main__":
    unittest.main()

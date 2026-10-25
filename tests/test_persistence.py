import json
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
        stopped_at_node=None,
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

    def test_run_store_rejects_unknown_schema_version(self) -> None:
        path = self.store.directory / "unknown-schema.json"
        path.write_text(
            '{"schema_version":2,"run_id":"unknown-schema",'
            '"status":"COMPLETED","outputs":{},"trace":[]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "invalid run record"):
            self.store.load("unknown-schema")

    def test_run_store_rejects_invalid_result_shapes(self) -> None:
        outputs_path = self.store.directory / "bad-outputs.json"
        outputs_path.write_text(
            '{"run_id":"bad-outputs","status":"COMPLETED",'
            '"outputs":[],"trace":[]}', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "invalid run record"):
            self.store.load("bad-outputs")

        trace_path = self.store.directory / "bad-trace-owner.json"
        trace_path.write_text(
            '{"run_id":"bad-trace-owner","status":"COMPLETED",'
            '"outputs":{},"trace":[{"run_id":"other","step":"s",'
            '"status":"COMPLETED","started_at":"2026-10-24T00:00:00+00:00",'
            '"duration_ms":1.0}]}', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "invalid run record"):
            self.store.load("bad-trace-owner")

        nested_path = self.store.directory / "bad-nested-output.json"
        nested_path.write_text(
            '{"run_id":"bad-nested-output","status":"COMPLETED",'
            '"outputs":{"step":{"value":{"bad":NaN}}},"trace":[]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "invalid run record"):
            self.store.load("bad-nested-output")

        trace_shape_path = self.store.directory / "bad-trace-shape.json"
        trace_shape_path.write_text(
            '{"run_id":"bad-trace-shape","status":"COMPLETED",'
            '"outputs":{},"trace":[{"run_id":"bad-trace-shape","step":"s",'
            '"status":"COMPLETED","started_at":"2026-10-24T00:00:00+00:00",'
            '"duration_ms":-1}]}', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "invalid run record"):
            self.store.load("bad-trace-shape")

        metadata_path = self.store.directory / "bad-trace-metadata.json"
        metadata_path.write_text(
            '{"run_id":"bad-trace-metadata","status":"COMPLETED",'
            '"outputs":{},"trace":[{"run_id":"bad-trace-metadata","step":"s",'
            '"status":"COMPLETED","started_at":"2026-10-24T00:00:00+00:00",'
            '"duration_ms":1.0,"state_version":-1,"metadata":[]}]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "invalid run record"):
            self.store.load("bad-trace-metadata")

    def test_list_runs_sorted(self) -> None:
        self.store.save(_sample_result("run-b"))
        self.store.save(_sample_result("run-a"))
        self.assertEqual(self.store.list_runs(), ["run-a", "run-b"])

    def test_legacy_stopped_at_key_is_read_compatible(self) -> None:
        self.store.save(_sample_result("legacy-run"))
        record_path = self.store.directory / "legacy-run.json"
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        payload["stopped_at"] = payload.pop("stopped_at_node")
        payload["stopped_at"] = "step_a"
        record_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = self.store.load("legacy-run")
        self.assertEqual(result.stopped_at_node, "step_a")
        self.assertEqual(result.stopped_at, "step_a")

    def test_save_overwrites_same_run_id(self) -> None:
        self.store.save(_sample_result("dup"))
        blocked = RunResult(
            run_id="dup",
            status=RunStatus.BLOCKED,
            outputs={},
            trace=(),
            stopped_at_node="step_a",
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
                stopped_at_node=stopped,
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

    def test_run_store_rejects_invalid_lifecycle_transition_log(self) -> None:
        path = Path(self._tmp.name) / "bad-transition.events.json"
        path.write_text(
            '{"run_id":"bad-transition","events":['
            '{"run_id":"bad-transition","sequence":1,"event_id":"e1",'
            '"event_type":"run.created","from_status":null,"to_status":"PENDING",'
            '"occurred_at":"2026-10-24T00:00:00+00:00"},'
            '{"run_id":"bad-transition","sequence":2,"event_id":"e2",'
            '"event_type":"run.completed","from_status":"PENDING","to_status":"COMPLETED",'
            '"occurred_at":"2026-10-24T00:00:01+00:00"}]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "invalid lifecycle event record"):
            self.store.load_lifecycle_events("bad-transition")

    def test_run_store_rejects_unknown_lifecycle_schema_version(self) -> None:
        path = Path(self._tmp.name) / "unknown-events.events.json"
        path.write_text(
            '{"schema_version":2,"run_id":"unknown-events","events":[]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "invalid lifecycle event record"):
            self.store.load_lifecycle_events("unknown-events")

    def test_run_store_serializes_concurrent_saves_without_partial_records(self) -> None:
        import threading

        results = [
            _sample_result(f"concurrent-{index}") for index in range(4)
        ]
        errors: list[Exception] = []

        def save(result: RunResult) -> None:
            try:
                self.store.save(result)
            except Exception as exc:
                errors.append(exc)

        workers = [threading.Thread(target=save, args=(result,)) for result in results]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(1)
        self.assertEqual(errors, [])
        for result in results:
            self.assertEqual(self.store.load(result.run_id), result)

    def test_run_store_writes_schema_version_to_lifecycle_log(self) -> None:
        from agent_core import LifecycleEvent, LifecycleStatus
        from datetime import datetime, timezone

        timestamp = datetime.now(timezone.utc).isoformat()
        events = (LifecycleEvent("schema-events", 1, "e1", "run.created", None,
                                 LifecycleStatus.PENDING, timestamp),)
        path = self.store.save_lifecycle_events("schema-events", events)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 1)

    def test_run_store_rejects_invalid_lifecycle_transition_on_save(self) -> None:
        from agent_core import LifecycleEvent, LifecycleStatus
        from datetime import datetime, timezone

        timestamp = datetime.now(timezone.utc).isoformat()
        events = (
            LifecycleEvent("bad-save", 1, "e1", "run.created", None,
                           LifecycleStatus.PENDING, timestamp),
            LifecycleEvent("bad-save", 2, "e2", "run.completed",
                           LifecycleStatus.PENDING, LifecycleStatus.COMPLETED, timestamp),
        )
        with self.assertRaisesRegex(ValueError, "lifecycle event transition is invalid"):
            self.store.save_lifecycle_events("bad-save", events)

    def test_run_store_rejects_empty_lifecycle_event_sequence(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            self.store.save_lifecycle_events("empty-events", ())

    def test_run_store_cleans_temporary_result_when_replace_fails(self) -> None:
        from pathlib import Path
        from unittest.mock import patch
        from agent_core import RunResult, RunStatus

        result = RunResult("replace-failure", RunStatus.COMPLETED, {}, ())
        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                self.store.save(result)
        self.assertEqual(list(Path(self._tmp.name).glob("*.tmp")), [])

    def test_run_store_rejects_result_status_mismatch_with_lifecycle(self) -> None:
        path = Path(self._tmp.name) / "status-mismatch.json"
        path.write_text(
            '{"run_id":"status-mismatch","status":"FAILED","outputs":{},"trace":[],'
            '"lifecycle_events":[{"run_id":"status-mismatch","sequence":1,"event_id":"e1",'
            '"event_type":"run.created","from_status":null,"to_status":"PENDING",'
            '"occurred_at":"2026-10-24T00:00:00+00:00"},'
            '{"run_id":"status-mismatch","sequence":2,"event_id":"e2",'
            '"event_type":"run.running","from_status":"PENDING","to_status":"RUNNING",'
            '"occurred_at":"2026-10-24T00:00:01+00:00"},'
            '{"run_id":"status-mismatch","sequence":3,"event_id":"e3",'
            '"event_type":"run.completed","from_status":"RUNNING","to_status":"COMPLETED",'
            '"occurred_at":"2026-10-24T00:00:02+00:00"}]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "invalid run record"):
            self.store.load("status-mismatch")

    def test_run_store_rejects_result_status_mismatch_on_save(self) -> None:
        from agent_core import LifecycleEvent, LifecycleStatus
        from datetime import datetime, timezone

        timestamp = datetime.now(timezone.utc).isoformat()
        result = RunResult(
            "status-mismatch-save", RunStatus.FAILED, {}, (),
            lifecycle_events=(
                LifecycleEvent("status-mismatch-save", 1, "e1", "run.created", None,
                               LifecycleStatus.PENDING, timestamp),
                LifecycleEvent("status-mismatch-save", 2, "e2", "run.running",
                               LifecycleStatus.PENDING, LifecycleStatus.RUNNING, timestamp),
                LifecycleEvent("status-mismatch-save", 3, "e3", "run.completed",
                               LifecycleStatus.RUNNING, LifecycleStatus.COMPLETED, timestamp),
            ),
        )
        with self.assertRaisesRegex(ValueError, "run status does not match"):
            self.store.save(result)


if __name__ == "__main__":
    unittest.main()

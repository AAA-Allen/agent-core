import unittest

from agent_core.lifecycle import LifecycleStatus, RunLifecycle


class LifecycleTests(unittest.TestCase):
    def test_valid_transitions_cover_pending_running_and_terminal_states(self) -> None:
        lifecycle = RunLifecycle("run-1")
        self.assertEqual(lifecycle.status, LifecycleStatus.PENDING)
        lifecycle.transition(LifecycleStatus.RUNNING)
        lifecycle.transition(LifecycleStatus.COMPLETED)
        self.assertTrue(lifecycle.terminal)
        self.assertEqual(
            [event.event_type for event in lifecycle.events],
            ["run.created", "run.running", "run.completed"],
        )
        self.assertEqual([event.sequence for event in lifecycle.events], [1, 2, 3])

    def test_blocked_failed_and_canceled_are_distinct_terminal_states(self) -> None:
        for target in (LifecycleStatus.BLOCKED, LifecycleStatus.FAILED, LifecycleStatus.CANCELED):
            lifecycle = RunLifecycle(f"run-{target.value.lower()}")
            lifecycle.transition(LifecycleStatus.RUNNING)
            event = lifecycle.transition(target, reason_code="STOPPED")
            self.assertEqual(lifecycle.status, target)
            self.assertEqual(event.to_status, target)
            self.assertEqual(event.reason_code, "STOPPED")

    def test_pending_can_be_canceled_without_running(self) -> None:
        lifecycle = RunLifecycle("run-1")
        lifecycle.transition(LifecycleStatus.CANCELED, reason_code="CANCEL_REQUESTED")
        self.assertEqual(lifecycle.status, LifecycleStatus.CANCELED)

    def test_terminal_and_invalid_transitions_are_rejected(self) -> None:
        lifecycle = RunLifecycle("run-1")
        with self.assertRaisesRegex(ValueError, "invalid lifecycle transition"):
            lifecycle.transition(LifecycleStatus.COMPLETED)
        lifecycle.transition(LifecycleStatus.RUNNING)
        lifecycle.transition(LifecycleStatus.FAILED)
        with self.assertRaisesRegex(ValueError, "invalid lifecycle transition"):
            lifecycle.transition(LifecycleStatus.RUNNING)

    def test_explicit_resume_is_the_only_terminal_reentry(self) -> None:
        lifecycle = RunLifecycle("run-1")
        lifecycle.transition(LifecycleStatus.RUNNING)
        lifecycle.transition(LifecycleStatus.FAILED, reason_code="MAX_STEPS_EXCEEDED")
        event = lifecycle.resume()
        self.assertEqual(event.event_type, "run.resumed")
        self.assertEqual(lifecycle.status, LifecycleStatus.RUNNING)
        with self.assertRaisesRegex(ValueError, "cannot resume"):
            RunLifecycle("run-2").resume()

    def test_rehydration_rejects_invalid_transition_history(self) -> None:
        from datetime import datetime, timezone
        from agent_core import LifecycleEvent

        events = (
            LifecycleEvent("run-1", 1, "e1", "run.created", None, LifecycleStatus.PENDING,
                           datetime.now(timezone.utc).isoformat()),
            LifecycleEvent("run-1", 2, "e2", "run.completed", LifecycleStatus.PENDING,
                           LifecycleStatus.COMPLETED, datetime.now(timezone.utc).isoformat()),
        )
        with self.assertRaisesRegex(ValueError, "transition"):
            RunLifecycle.from_events(events)

    def test_rehydration_rejects_event_type_mismatch(self) -> None:
        from datetime import datetime, timezone
        from agent_core import LifecycleEvent

        timestamp = datetime.now(timezone.utc).isoformat()
        events = (
            LifecycleEvent("run-1", 1, "e1", "run.created", None, LifecycleStatus.PENDING, timestamp),
            LifecycleEvent("run-1", 2, "e2", "run.wrong", LifecycleStatus.PENDING,
                           LifecycleStatus.RUNNING, timestamp),
        )
        with self.assertRaisesRegex(ValueError, "transition"):
            RunLifecycle.from_events(events)

    def test_rehydration_rejects_duplicate_event_ids(self) -> None:
        from datetime import datetime, timezone
        from agent_core import LifecycleEvent

        timestamp = datetime.now(timezone.utc).isoformat()
        events = (
            LifecycleEvent("run-1", 1, "same", "run.created", None,
                           LifecycleStatus.PENDING, timestamp),
            LifecycleEvent("run-1", 2, "same", "run.running", LifecycleStatus.PENDING,
                           LifecycleStatus.RUNNING, timestamp),
        )
        with self.assertRaisesRegex(ValueError, "event ids must be unique"):
            RunLifecycle.from_events(events)

    def test_rehydration_rejects_empty_event_timestamp(self) -> None:
        from agent_core import LifecycleEvent

        events = (
            LifecycleEvent("run-1", 1, "e1", "run.created", None,
                           LifecycleStatus.PENDING, ""),
        )
        with self.assertRaisesRegex(ValueError, "occurred_at must be nonempty"):
            RunLifecycle.from_events(events)

    def test_rehydration_rejects_invalid_event_timestamp(self) -> None:
        from agent_core import LifecycleEvent

        events = (
            LifecycleEvent("run-1", 1, "e1", "run.created", None,
                           LifecycleStatus.PENDING, "yesterday"),
        )
        with self.assertRaisesRegex(ValueError, "occurred_at must be ISO-8601"):
            RunLifecycle.from_events(events)

    def test_rehydration_rejects_invalid_event_reason_code(self) -> None:
        from agent_core import LifecycleEvent
        from datetime import datetime, timezone

        events = (
            LifecycleEvent("run-1", 1, "e1", "run.created", None,
                           LifecycleStatus.PENDING, datetime.now(timezone.utc).isoformat(), " "),
        )
        with self.assertRaisesRegex(ValueError, "reason_code must be nonempty"):
            RunLifecycle.from_events(events)

    def test_rehydration_rejects_string_status_values(self) -> None:
        from agent_core import LifecycleEvent
        from datetime import datetime, timezone

        events = (
            LifecycleEvent("run-1", 1, "e1", "run.created", None,
                           "PENDING", datetime.now(timezone.utc).isoformat()),
        )
        with self.assertRaisesRegex(ValueError, "to_status is invalid"):
            RunLifecycle.from_events(events)

    def test_explicit_recovery_records_running_recovery(self) -> None:
        lifecycle = RunLifecycle("run-1")
        lifecycle.transition(LifecycleStatus.RUNNING)
        event = lifecycle.recover()
        self.assertEqual(event.event_type, "run.recovered")
        self.assertEqual(lifecycle.status, LifecycleStatus.RUNNING)


if __name__ == "__main__":
    unittest.main()

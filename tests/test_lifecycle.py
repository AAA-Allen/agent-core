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
            ["run.running", "run.completed"],
        )
        self.assertEqual([event.sequence for event in lifecycle.events], [1, 2])

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


if __name__ == "__main__":
    unittest.main()

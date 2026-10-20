"""Tests for the example application: it must keep proving domain neutrality."""

import unittest

from agent_core import RunStatus
from examples.text_pipeline import build_text_pipeline


class TextPipelineExampleTests(unittest.TestCase):
    def test_normal_text_completes_and_classifies(self) -> None:
        result = build_text_pipeline().run(
            {"text": "  Hello   world  "}, run_id="ex-normal"
        )
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.outputs["normalize"]["text"], "Hello world")
        self.assertEqual(result.outputs["analyze"]["word_count"], 2)
        self.assertEqual(result.outputs["classify"]["length_class"], "short")

    def test_empty_text_is_blocked_with_reason_code(self) -> None:
        result = build_text_pipeline().run({"text": "   "}, run_id="ex-empty")
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.stopped_at, "validate")
        self.assertEqual(result.reason_code, "EMPTY_TEXT")
        self.assertNotIn("analyze", result.outputs)

    def test_long_text_classifies_long(self) -> None:
        result = build_text_pipeline().run(
            {"text": "word " * 30}, run_id="ex-long"
        )
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.outputs["classify"]["length_class"], "long")

    def test_trace_records_every_step(self) -> None:
        result = build_text_pipeline().run({"text": "a b c"}, run_id="ex-trace")
        self.assertEqual(
            [event.step for event in result.trace],
            ["normalize", "validate", "analyze", "classify"],
        )


if __name__ == "__main__":
    unittest.main()

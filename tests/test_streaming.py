"""Token streaming bus and llm-node streaming path contract tests."""

import json
import threading
import unittest

from agent_core import RunStatus, Runtime
from agent_core.models import ModelRequestError
from agent_core.nodes import build_agent_from_config
from agent_core.streaming import TokenBus

LLM_CONFIG = {
    "name": "asker",
    "entry": "ask",
    "nodes": [
        {"id": "ask", "type": "llm", "params": {"prompt": "问题:{q}", "into": "answer"}},
    ],
}


class StreamingModel:
    """Implements the optional complete_stream convention."""

    def __init__(self, chunks=("你", "好")):
        self.chunks = chunks
        self.calls = []

    def complete(self, messages):
        self.calls.append(("complete", messages))
        return {"content": "".join(self.chunks)}

    def complete_stream(self, messages, on_delta=None):
        self.calls.append(("stream", messages))
        for chunk in self.chunks:
            if on_delta is not None:
                on_delta(chunk)
        return {"content": "".join(self.chunks), "usage": {}}


class FailingStreamModel:
    def complete(self, messages):
        return {"content": "unused"}

    def complete_stream(self, messages, on_delta=None):
        raise ModelRequestError("MODEL_TIMEOUT", "timed out mid-stream")


class TokenBusTests(unittest.TestCase):
    def test_publish_history_replay_and_close(self) -> None:
        bus = TokenBus()
        bus.open("run-1")
        bus.publish("run-1", "ask", "a")
        bus.publish("run-1", "ask", "b")
        events, truncated, closed = bus.history("run-1")
        self.assertEqual(events, [("ask", "a"), ("ask", "b")])
        self.assertFalse(truncated)
        self.assertFalse(closed)
        # 增量读取:after=1 只看到第二条
        self.assertEqual(bus.history("run-1", after=1)[0], [("ask", "b")])
        bus.close("run-1")
        self.assertTrue(bus.history("run-1")[2])
        # 关闭后发布被忽略
        bus.publish("run-1", "ask", "c")
        self.assertEqual(len(bus.history("run-1")[0]), 2)
        # 未知 run
        self.assertEqual(bus.history("missing"), ([], False, False))

    def test_history_cap_marks_truncated(self) -> None:
        bus = TokenBus(max_history=2)
        bus.open("run-cap")
        for text in ("a", "b", "c"):
            bus.publish("run-cap", "n", text)
        events, truncated, _ = bus.history("run-cap")
        self.assertEqual(events, [("n", "b"), ("n", "c")])
        self.assertTrue(truncated)

    def test_publish_without_open_is_dropped(self) -> None:
        bus = TokenBus()
        bus.publish("ghost", "n", "x")
        self.assertEqual(bus.history("ghost"), ([], False, False))


class LlmStreamingTests(unittest.TestCase):
    def test_streaming_model_publishes_to_bus_and_assembles(self) -> None:
        model = StreamingModel(chunks=("确定", "性", "运行时"))
        agent = build_agent_from_config(LLM_CONFIG, runtime=Runtime(), model=model)
        result = agent.run({"q": "x"}, run_id="stream-run")
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state["answer"], "确定性运行时")
        self.assertEqual(model.calls[0][0], "stream")
        from agent_core.streaming import default_bus

        events, _truncated, closed = default_bus().history("stream-run")
        self.assertEqual([text for _node, text in events], ["确定", "性", "运行时"])
        self.assertEqual(events[0][0], "ask")
        self.assertTrue(closed)

    def test_non_streaming_model_falls_back_to_complete(self) -> None:
        class PlainModel:
            def complete(self, messages):
                return {"content": "整段回复"}

        agent = build_agent_from_config(LLM_CONFIG, runtime=Runtime(), model=PlainModel())
        result = agent.run({"q": "x"}, run_id="plain-run")
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state["answer"], "整段回复")

    def test_stream_failure_maps_reason_code_and_closes_bus(self) -> None:
        agent = build_agent_from_config(LLM_CONFIG, runtime=Runtime(), model=FailingStreamModel())
        result = agent.run({"q": "x"}, run_id="fail-run")
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "MODEL_TIMEOUT")
        from agent_core.streaming import default_bus

        self.assertTrue(default_bus().history("fail-run")[2])


if __name__ == "__main__":
    unittest.main()

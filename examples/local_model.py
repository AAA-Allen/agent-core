"""Integrating a self-trained or local model without any HTTP layer.

``ModelAdapter`` is a structural protocol: any object with a
``complete(messages) -> Mapping`` method qualifies, and
``complete`` is a plain in-process Python call — no HTTP, no SDK, no
inheritance. The ``llm`` node only relies on this contract:

- the return value is a mapping with a string ``content``;
- failures raise; an exception carrying ``reason_code`` is reported as that
  FAILED reason code, anything else becomes ``MODEL_REQUEST_FAILED``.

Run this file directly to see the in-process path work end to end::

    python -m examples.local_model

To serve it over HTTP instead, keep this class and write a tiny launcher::

    from agent_core.server import create_server
    from examples.local_model import LocalTrainedModel, AGENT_CONFIG

    server = create_server(model=LocalTrainedModel())
    # 然后照常 --config 装配使用 llm 节点的 Agent
"""

from agent_core import Runtime
from agent_core.nodes import build_agent_from_config

AGENT_CONFIG = {
    "name": "local-assistant",
    "instructions": "使用自训练本地模型回答用户问题。",
    "description": "Demo agent wired to an in-process self-trained model.",
    "entry": "intake",
    "nodes": [
        {"id": "intake", "type": "validate_input",
         "params": {"field": "task", "blocked_reason": "EMPTY_TASK", "route": "ask"}},
        {"id": "ask", "type": "llm",
         "params": {"system": "你是自训练模型,简洁回答。", "prompt": "问题:{task}", "into": "answer"}},
    ],
    "edges": [{"source": "intake", "target": "ask", "when": "ask"}],
}


class LocalTrainedModel:
    """Wrap a self-trained model; replace the body of ``complete``."""

    def __init__(self) -> None:
        # 在这里加载你的 checkpoint / 权重 / 分词器,例如:
        # self._model = my_loader("checkpoints/best.pt")
        pass

    def complete(self, messages):
        user_text = messages[-1]["content"]
        # 换成你的真实推理调用,例如:
        # tokens = self._tokenizer.encode(user_text)
        # output = self._model.generate(tokens)
        # return {"content": self._tokenizer.decode(output)}
        return {"content": f"(本地模型)收到:{user_text}"}


class FailingLocalModel:
    """演示失败契约:携带 reason_code 的异常会被节点如实报告."""

    class _RequestFailed(Exception):
        reason_code = "LOCAL_MODEL_OOM"

    def complete(self, messages):
        raise self._RequestFailed("显存不足")


def main() -> None:
    runtime = Runtime()
    agent = build_agent_from_config(AGENT_CONFIG, runtime=runtime, model=LocalTrainedModel())
    result = agent.run({"task": "你好"}, run_id="local-model-demo")
    print("status:", result.status.value)
    print("answer:", result.state.get("answer"))

    failing = build_agent_from_config(AGENT_CONFIG, runtime=runtime, model=FailingLocalModel())
    blocked = failing.run({"task": "你好"}, run_id="local-model-fail")
    print("failing status:", blocked.status.value, "| reason:", blocked.reason_code)


if __name__ == "__main__":
    main()

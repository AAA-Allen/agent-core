"""Declarative node factory library contract tests."""

import json
import tempfile
import unittest
from pathlib import Path

from agent_core import AgentBuilder, NodeResult, RunStatus, Runtime, Tool, ToolRegistry
from agent_core.nodes import (
    NODE_FACTORIES,
    build_agent_from_config,
    register_node_factory,
)

WORKBENCH_CONFIG = {
    "name": "config-agent",
    "instructions": "配置装配的演示 Agent",
    "description": "assembled from nodes.py",
    "metadata": {"kind": "declarative"},
    "entry": "intake",
    "nodes": [
        {"id": "intake", "type": "validate_input",
         "params": {"field": "task", "blocked_reason": "EMPTY_TASK", "route": "plan"}},
        {"id": "plan", "type": "split_steps",
         "params": {"field": "task", "into": "plan", "count_key": "step_count", "route": "stats"}},
        {"id": "stats", "type": "tool",
         "params": {"name": "text_stats", "arguments": {"task": "{task}"}, "output_key": "text_stats", "route": "summary"}},
        {"id": "summary", "type": "template",
         "params": {"updates": {"summary": "任务“{task}”已拆解为 {step_count} 个步骤。"}}},
    ],
    "edges": [
        {"source": "intake", "target": "plan", "when": "plan"},
        {"source": "plan", "target": "stats", "when": "stats"},
        {"source": "stats", "target": "summary", "when": "summary"},
    ],
}


def demo_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool("text_stats", lambda args: {"chars": len(args["task"]), "words": len(args["task"].split())})
    )
    return registry


class ConfigAssemblyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Runtime()

    def test_config_agent_completes_with_expected_state(self) -> None:
        agent = build_agent_from_config(WORKBENCH_CONFIG, runtime=self.runtime, tools=demo_tools())
        result = agent.run({"task": "整理项目进展;准备评审"})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state["step_count"], 2)
        self.assertEqual(result.state["text_stats"], {"chars": 11, "words": 1})
        self.assertEqual(result.state["summary"], "任务“整理项目进展;准备评审”已拆解为 2 个步骤。")
        self.assertEqual(agent.describe()["entry"], "intake")
        self.assertEqual(agent.spec.metadata, {"kind": "declarative"})

    def test_config_agent_blocks_on_empty_field(self) -> None:
        agent = build_agent_from_config(WORKBENCH_CONFIG, runtime=self.runtime, tools=demo_tools())
        result = agent.run({"task": "   "})
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "EMPTY_TASK")
        self.assertEqual(result.stopped_at, "intake")

    def test_template_missing_state_key_fails_closed(self) -> None:
        config = {
            "name": "template-agent",
            "entry": "render",
            "nodes": [
                {"id": "render", "type": "template",
                 "params": {"updates": {"text": "你好 {missing_key}"}}},
            ],
        }
        agent = build_agent_from_config(config, runtime=self.runtime)
        result = agent.run({})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "TEMPLATE_KEY_MISSING")

    def test_template_rejects_attribute_access_placeholders(self) -> None:
        config = {
            "name": "template-agent",
            "entry": "render",
            "nodes": [
                {"id": "render", "type": "template",
                 "params": {"updates": {"text": "{task.__class__}"}}},
            ],
        }
        agent = build_agent_from_config(config, runtime=self.runtime)
        result = agent.run({"task": "hello"})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "TEMPLATE_KEY_MISSING")

    def test_tool_node_without_registry_fails_closed(self) -> None:
        config = {
            "name": "tool-agent",
            "entry": "call",
            "nodes": [
                {"id": "call", "type": "tool",
                 "params": {"name": "text_stats", "arguments": {}}},
            ],
        }
        agent = build_agent_from_config(config, runtime=self.runtime)
        result = agent.run({})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "TOOL_REGISTRY_MISSING")

    def test_tool_node_reports_missing_argument_key(self) -> None:
        config = {
            "name": "tool-agent",
            "entry": "call",
            "nodes": [
                {"id": "call", "type": "tool",
                 "params": {"name": "text_stats", "arguments": {"task": "{absent}"}}},
            ],
        }
        agent = build_agent_from_config(config, runtime=self.runtime, tools=demo_tools())
        result = agent.run({})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "TOOL_ARGUMENT_MISSING")

    def test_route_by_value_selects_and_falls_back(self) -> None:
        config = {
            "name": "router-agent",
            "entry": "route",
            "nodes": [
                {"id": "route", "type": "route_by_value",
                 "params": {"field": "lane", "routes": {"fast": "quick", "slow": "thorough"},
                            "default_route": "thorough"}},
                {"id": "quick", "type": "template", "params": {"updates": {"path": "quick"}}},
                {"id": "thorough", "type": "template", "params": {"updates": {"path": "thorough"}}},
            ],
            "edges": [
                {"source": "route", "target": "quick", "when": "quick"},
                {"source": "route", "target": "thorough", "when": "thorough"},
            ],
        }
        agent = build_agent_from_config(config, runtime=self.runtime)
        self.assertEqual(agent.run({"lane": "fast"}).state["path"], "quick")
        self.assertEqual(agent.run({"lane": "unknown"}).state["path"], "thorough")
        self.assertEqual(agent.run({}).state["path"], "thorough")

    def test_route_by_value_without_default_blocks(self) -> None:
        config = {
            "name": "router-agent",
            "entry": "route",
            "nodes": [
                {"id": "route", "type": "route_by_value",
                 "params": {"field": "lane", "routes": {"fast": "quick"}}},
                {"id": "quick", "type": "template", "params": {"updates": {"path": "quick"}}},
            ],
            "edges": [{"source": "route", "target": "quick", "when": "quick"}],
        }
        agent = build_agent_from_config(config, runtime=self.runtime)
        blocked = agent.run({})
        self.assertEqual(blocked.status, RunStatus.BLOCKED)
        self.assertEqual(blocked.reason_code, "ROUTE_UNRESOLVED")

    def test_subagent_delegates_and_hoists_summary(self) -> None:
        delegate_config = {
            "name": "support-triage",
            "entry": "intake",
            "nodes": [
                {"id": "intake", "type": "validate_input",
                 "params": {"field": "task", "blocked_reason": "EMPTY_TASK", "route": "render"}},
                {"id": "render", "type": "template",
                 "params": {"updates": {"summary": "子处理:{task}"}}},
            ],
            "edges": [{"source": "intake", "target": "render", "when": "render"}],
        }
        runtime = Runtime()
        delegate = build_agent_from_config(delegate_config, runtime=runtime)
        orchestrator_config = {
            "name": "orchestrator",
            "entry": "intake",
            "nodes": [
                {"id": "intake", "type": "validate_input",
                 "params": {"field": "task", "blocked_reason": "EMPTY_TASK", "route": "delegate"}},
                {"id": "delegate", "type": "subagent",
                 "params": {"agent": "support-triage", "input": {"task": "{task}"},
                            "output_key": "delegated", "summary_into": "delegated_summary",
                            "route": "render"}},
                {"id": "render", "type": "template",
                 "params": {"updates": {"summary": "委派完成:{delegated_summary}"}}},
            ],
            "edges": [
                {"source": "intake", "target": "delegate", "when": "delegate"},
                {"source": "delegate", "target": "render", "when": "render"},
            ],
        }
        agent = build_agent_from_config(
            orchestrator_config, runtime=runtime,
            agent_resolver=lambda name, memory=None: delegate if name == "support-triage" else None,
        )
        result = agent.run({"task": "修复工单"})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state["delegated_summary"], "子处理:修复工单")
        self.assertEqual(result.state["summary"], "委派完成:子处理:修复工单")
        delegated = result.state["delegated"]
        self.assertEqual(delegated["status"], "COMPLETED")
        self.assertTrue(delegated["run_id"])
        self.assertNotEqual(delegated["run_id"], result.run_id)

    def test_subagent_propagates_blocked_and_missing(self) -> None:
        blocked_delegate = (
            AgentBuilder("blocked-delegate")
            .add_node("gate", lambda ctx: NodeResult.blocked("DELEGATE_GATE"))
            .set_entry("gate")
            .build(runtime=Runtime())
        )
        runtime = Runtime()
        config = {
            "name": "caller",
            "entry": "call",
            "nodes": [
                {"id": "call", "type": "subagent",
                 "params": {"agent": "blocked-delegate", "input": {}}},
            ],
        }
        agent = build_agent_from_config(
            config, runtime=runtime,
            agent_resolver=lambda name, memory=None: blocked_delegate,
        )
        result = agent.run({})
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "DELEGATE_GATE")

        missing = build_agent_from_config(config, runtime=runtime, agent_resolver=lambda name, memory=None: None)
        result = missing.run({})
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "SUBAGENT_NOT_FOUND")

    def test_subagent_requires_resolver_at_assembly(self) -> None:
        config = {
            "name": "caller",
            "entry": "call",
            "nodes": [{"id": "call", "type": "subagent", "params": {"agent": "any"}}],
        }
        with self.assertRaises(ValueError):
            build_agent_from_config(config, runtime=Runtime())

    def test_llm_node_replies_and_fails_closed(self) -> None:
        class FakeModel:
            def __init__(self, error=None):
                self.calls = []
                self.error = error

            def complete(self, messages, *, tools=None, context=None):
                self.calls.append(messages)
                if self.error:
                    raise self.error
                return {"content": f"回答:{messages[-1]['content']}"}

        from agent_core.models import ModelRequestError

        config = {
            "name": "asker",
            "entry": "ask",
            "nodes": [
                {"id": "ask", "type": "llm",
                 "params": {"system": "简洁回答", "prompt": "问题:{q}", "into": "answer"}},
            ],
        }

        # 未绑定模型 → BLOCKED,不触网
        agent = build_agent_from_config(config, runtime=Runtime())
        result = agent.run({"q": "你好"})
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "MODEL_NOT_CONFIGURED")

        # 正常回复写进状态
        model = FakeModel()
        agent = build_agent_from_config(config, runtime=Runtime(), model=model)
        result = agent.run({"q": "你好"})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state["answer"], "回答:问题:你好")
        self.assertEqual(model.calls[0][0], {"role": "system", "content": "简洁回答"})

        # 传输失败 → FAILED 带原因码
        agent = build_agent_from_config(
            config, runtime=Runtime(),
            model=FakeModel(error=ModelRequestError("MODEL_HTTP_ERROR", "http 500")),
        )
        result = agent.run({"q": "你好"})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "MODEL_HTTP_ERROR")

        # 模板缺键 → FAILED
        result = build_agent_from_config(config, runtime=Runtime(), model=FakeModel()).run({})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "PROMPT_KEY_MISSING")

    def test_tool_node_capabilities_must_be_declared(self) -> None:
        registry = ToolRegistry()
        registry.register(
            Tool("guarded", lambda args: {"ok": True}, required_capabilities=frozenset({"restricted"}))
        )
        config = {
            "name": "caller",
            "entry": "call",
            "nodes": [{"id": "call", "type": "tool", "params": {"name": "guarded", "arguments": {}}}],
        }
        # 未声明能力 → BLOCKED,工具未被调用
        agent = build_agent_from_config(config, runtime=Runtime(), tools=registry)
        result = agent.run({})
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "CAPABILITY_DENIED")
        # 显式声明 → 成功
        config["nodes"][0]["params"]["capabilities"] = ["restricted"]
        agent = build_agent_from_config(config, runtime=Runtime(), tools=registry)
        result = agent.run({})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state["guarded"], {"ok": True})
        # 声明不覆盖所需能力 → 仍拒绝
        config["nodes"][0]["params"]["capabilities"] = ["other"]
        agent = build_agent_from_config(config, runtime=Runtime(), tools=registry)
        result = agent.run({})
        self.assertEqual(result.reason_code, "CAPABILITY_DENIED")
        # 非法能力参数 → 装配期拒绝
        config["nodes"][0]["params"]["capabilities"] = "restricted"
        with self.assertRaises(ValueError):
            build_agent_from_config(config, runtime=Runtime(), tools=registry)

    def test_example_config_file_runs_end_to_end(self) -> None:
        config = json.loads(Path("examples/support_agent.json").read_text(encoding="utf-8"))
        agent = build_agent_from_config(config, runtime=self.runtime, tools=demo_tools())
        result = agent.run({"task": "无法登录;密码重置邮件未收到"})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state["step_count"], 2)
        self.assertIn("已拆解为 2 个步骤", result.state["summary"])


class ConfigValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Runtime()

    def test_rejects_invalid_configs(self) -> None:
        base = dict(WORKBENCH_CONFIG)
        cases = [
            ("no name", {**base, "name": "  "}),
            ("no nodes", {**base, "nodes": []}),
            ("unknown type", {**base, "nodes": [{**base["nodes"][0], "type": "magic"}]}),
            ("duplicate id", {**base, "nodes": [base["nodes"][0], base["nodes"][0]]}),
            ("entry unknown", {**base, "entry": "ghost"}),
            ("edge unknown target", {**base, "edges": [{"source": "intake", "target": "ghost"}]}),
            ("bad params", {**base, "nodes": [
                {"id": "intake", "type": "validate_input", "params": {"field": ""}},
            ]}),
            ("nodes not list", {**base, "nodes": "intake"}),
        ]
        for label, config in cases:
            with self.assertRaises(ValueError, msg=label):
                build_agent_from_config(config, runtime=self.runtime)

    def test_factory_param_validation_at_assembly_time(self) -> None:
        with self.assertRaises(ValueError):
            NODE_FACTORIES["split_steps"]({"field": "task", "max_length": -1})
        with self.assertRaises(ValueError):
            NODE_FACTORIES["template"]({"updates": {}})
        with self.assertRaises(ValueError):
            NODE_FACTORIES["route_by_value"]({"field": "lane", "routes": {}})

    def test_register_node_factory_and_duplicate_guard(self) -> None:
        calls: list[str] = []

        def upper_node(params):
            field = params["field"]

            def execute(ctx):
                calls.append(ctx.input[field])
                return NodeResult.success({field: str(ctx.input[field]).upper()})

            return execute

        try:
            register_node_factory("upper_case", upper_node)
            with self.assertRaises(ValueError):
                register_node_factory("upper_case", upper_node)
            config = {
                "name": "custom-agent",
                "entry": "upper",
                "nodes": [{"id": "upper", "type": "upper_case", "params": {"field": "word"}}],
            }
            agent = build_agent_from_config(config, runtime=self.runtime)
            result = agent.run({"word": "hello"})
            self.assertEqual(result.status, RunStatus.COMPLETED)
            self.assertEqual(result.state["word"], "HELLO")
            self.assertEqual(calls, ["hello"])
        finally:
            NODE_FACTORIES.pop("upper_case", None)


class AgentLoopTests(unittest.TestCase):
    LOOP_CONFIG = {
        "name": "tool-agent",
        "entry": "loop",
        "nodes": [{
            "id": "loop", "type": "agent_loop",
            "params": {
                "prompt": "问题:{q}",
                "tools": [{"name": "echo", "description": "回显输入", "parameters": {"value": "string"}}],
                "capabilities": ["basic"],
                "max_rounds": 3,
                "into": "answer",
            },
        }],
    }

    def setUp(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register(Tool("echo", lambda args: {"echo": args["value"]}))
        self.runtime = Runtime()

    def build(self, model, config=None):
        return build_agent_from_config(config or self.LOOP_CONFIG, runtime=self.runtime,
                                       tools=self.registry, model=model)

    def test_loop_calls_tool_then_answers_with_audit(self) -> None:
        class Model:
            def __init__(self):
                self.calls = []

            def complete_with_tools(self, messages, tool_schemas):
                self.calls.append(([dict(m) for m in messages], [dict(s) for s in tool_schemas]))
                if len(self.calls) == 1:
                    return {"tool_calls": [{"id": "call-1", "name": "echo", "arguments": {"value": "hi"}}]}
                return {"content": "工具说:hi"}

        model = Model()
        result = self.build(model).run({"q": "测试"})
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.state["answer"], "工具说:hi")
        self.assertEqual(result.state["answer_rounds"],
                         [{"tool": "echo", "arguments": {"value": "hi"}, "output": {"echo": "hi"}}])
        # 第二轮的 messages 含 assistant 工具调用与 tool 结果
        second_messages = model.calls[1][0]
        self.assertEqual(second_messages[-2]["role"], "assistant")
        self.assertEqual(second_messages[-2]["tool_calls"][0]["function"]["name"], "echo")
        self.assertEqual(second_messages[-1]["role"], "tool")
        self.assertEqual(second_messages[-1]["tool_call_id"], "call-1")
        # 模型看到声明的 schema
        self.assertEqual(model.calls[0][1][0]["name"], "echo")

    def test_capability_must_be_declared(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register(Tool("echo", lambda args: {"echo": args["value"]},
                                    required_capabilities=frozenset({"secret"})))

        class Model:
            def complete_with_tools(self, messages, schemas):
                return {"tool_calls": [{"id": "c1", "name": "echo", "arguments": {"value": "x"}}]}

        result = self.build(Model()).run({"q": "x"})
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "CAPABILITY_DENIED")

    def test_unknown_tool_blocks(self) -> None:
        class Model:
            def complete_with_tools(self, messages, schemas):
                return {"tool_calls": [{"id": "c1", "name": "ghost", "arguments": {}}]}

        result = self.build(Model()).run({"q": "x"})
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "TOOL_NOT_FOUND")

    def test_tool_failure_propagates_reason_code(self) -> None:
        class Boom(Exception):
            reason_code = "ECHO_EXPLODED"

        self.registry = ToolRegistry()
        self.registry.register(Tool("echo", lambda args: (_ for _ in ()).throw(Boom())))

        class Model:
            def complete_with_tools(self, messages, schemas):
                return {"tool_calls": [{"id": "c1", "name": "echo", "arguments": {}}]}

        result = self.build(Model()).run({"q": "x"})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "ECHO_EXPLODED")

    def test_max_rounds_exhaustion_fails(self) -> None:
        class Model:
            def complete_with_tools(self, messages, schemas):
                return {"tool_calls": [{"id": "call-again", "name": "echo", "arguments": {"value": "x"}}]}

        result = self.build(Model()).run({"q": "x"})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "MAX_TOOL_ROUNDS")

    def test_model_without_tool_support_blocks(self) -> None:
        class PlainModel:
            def complete(self, messages):
                return {"content": "no tools here"}

        result = self.build(PlainModel()).run({"q": "x"})
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.reason_code, "MODEL_TOOLS_UNSUPPORTED")

    def test_model_error_maps_reason_code(self) -> None:
        from agent_core.models import ModelRequestError

        class Model:
            def complete_with_tools(self, messages, schemas):
                raise ModelRequestError("MODEL_TIMEOUT", "timed out")

        result = self.build(Model()).run({"q": "x"})
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.reason_code, "MODEL_TIMEOUT")

    def test_assembly_validates_params(self) -> None:
        for bad in (
            {"tools": []},
            {"tools": "echo"},
            {"tools": [{"name": ""}]},
            {"tools": [{"name": "echo", "parameters": "x"}]},
            {"capabilities": "basic"},
            {"max_rounds": 0},
            {"max_rounds": True},
        ):
            config = {
                "name": "bad", "entry": "loop",
                "nodes": [{"id": "loop", "type": "agent_loop",
                           "params": {"prompt": "p", **bad}}],
            }
            with self.assertRaises(ValueError, msg=bad):
                build_agent_from_config(config, runtime=Runtime(), tools=self.registry)


if __name__ == "__main__":
    unittest.main()

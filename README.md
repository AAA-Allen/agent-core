# agent-core

领域中立（domain-neutral）的 Agent 工作流运行时。2026-10-20 从 ICT 晶圆质检项目蒸馏而来：晶圆检测只是首个应用，不进入框架核心类型。

## 它负责什么

框架核心负责：步骤编排、运行标识（`run_id`）、上下文传递、工具注册与逻辑能力检查、显式阻断、异常收敛和结构化执行记录。应用负责：模型、提示词、具体工具实现、业务证据规则、路由阈值和人工复核语义。

差异化方向是**可审查的决策边界**：每一步显式返回 `COMPLETED` 或带原因码的 `BLOCKED`；异常进入 `FAILED`，后续步骤不运行；Trace 记录执行事实，不把模拟预览当真实执行。

## 核心类型

| 类型 | 责任 |
|---|---|
| `Step` | 注册唯一名称和执行函数 |
| `RunContext` | 只读输入、已完成步骤的输出快照及 `run_id` |
| `StepOutcome` | 显式完成或阻断，阻断必须有原因码 |
| `WorkflowRunner` | 顺序执行，遇阻断或异常立即停止 |
| `TraceEvent` / `RunResult` | 保存步骤状态、UTC 开始时间、耗时、错误类型与最终状态 |
| `Tool` / `ToolRegistry` | 注册唯一工具名，检查调用方声明的能力集合，统一返回成功、阻断或失败 |
| `tool_step` | 将工具调用包装为工作流步骤，使工具状态进入同一条执行 Trace |
| `RunStore` | 把已结束的运行（完成、阻断、失败）按 run_id 落盘为单个 JSON 文件，原子写入，可读回 |

输入与步骤输出要求是 JSON 可序列化对象。运行时复制快照，避免后续步骤修改前一步的记录。Trace 不保存异常消息，防止泄露工具返回的敏感内容。

## 最小用法

```python
from agent_core import Tool, ToolRegistry, WorkflowRunner, tool_step

registry = ToolRegistry()
registry.register(Tool("normalize", lambda args: {"text": args["text"].strip()}))
workflow = WorkflowRunner([
    tool_step("normalize_input", "normalize", registry,
              lambda context: {"text": context.input["text"]}),
])
result = workflow.run({"text": "  hello  "})
# result.status == RunStatus.COMPLETED
# result.outputs["normalize_input"]["text"] == "hello"
```

## 运行测试

```bash
python -m pytest tests/ -q
```

## 示例应用

`examples/text_pipeline.py`：纯文本领域的 normalize → validate → analyze → classify 管线。空文本在 `validate` 处 BLOCKED（原因码 `EMPTY_TEXT`）——证明框架核心不含任何业务词汇，同一运行时可跑任意领域。

```bash
python -m examples.text_pipeline
```

## v0.1 Graph Runtime

v0.1 在保留 `WorkflowRunner`、`Step`、`StepOutcome` 和 `ToolRegistry` 的基础上，提供图执行 API：

```python
from agent_core import Graph, NodeResult, Runtime

graph = (
    Graph()
    .add_node("validate", lambda ctx: NodeResult.success({"valid": True}, route="next"))
    .add_node("finish", lambda ctx: NodeResult.success({"done": True}))
    .set_entry("validate")
    .add_edge("validate", "finish", when="next")
    .compile()
)
result = Runtime().run(graph, input={"text": "hello"})
```

图节点只能返回 `NodeResult`，由 Runtime 验证并合并 `updates`。节点状态为
`NodeStatus.SUCCEEDED`、`BLOCKED` 或 `FAILED`；阻断和失败会停止当前运行。图运行支持条件路由、静态校验、最大执行步数和统一 Trace。`simulated=True` 会保留在最终结果中，不能当作真实工具执行证据。

当前 v0.1 已包含线性图、条件路由、状态增量、Trace、工具适配和最大步数限制；不包含 Checkpoint、恢复、重试、取消、并行、LLM 或多 Agent 调度。

v0.2 的第一步提供 `Checkpoint`、`CheckpointStore` 和 `InMemoryCheckpointStore` 快照协议。将 `checkpoint_store` 传给 `Runtime` 后，每次成功节点边界会保存状态、状态版本、下一节点和执行步数；`load_for()` 会校验快照所属的 `graph_id`。它们只保存可恢复状态，不自动恢复运行，也不承诺 exactly-once 执行。

### 公共 API

```python
from agent_core import Graph, NodeResult, NodeStatus, Runtime

def validate(ctx):
    if not ctx.input.get("text"):
        return NodeResult.blocked("EMPTY_TEXT")
    return NodeResult.success({"valid": True}, route="valid")

graph = (
    Graph()
    .add_node("validate", validate)
    .add_node("finish", lambda ctx: NodeResult.success({"done": True}))
    .set_entry("validate")
    .add_edge("validate", "finish", when="valid")
    .compile()
)
result = Runtime().run(graph, {"text": "hello"})
assert result.status.value == "COMPLETED"
```

节点通过 `ctx.state` 读取当前状态，通过 `NodeResult.success(updates, route=...)` 请求状态更新和路由。Runtime 负责校验、合并和 Trace；节点不能直接修改共享状态。

## 后续能力边界

- 没有 LLM 自主规划、多 Agent 协商、并行节点、人工等待节点
- 没有运行恢复、取消、超时强制中断、重试、调用预算、幂等键
- 工具能力集合是**进程内逻辑约束**，不是操作系统沙箱或真正的身份鉴权
- 持久化只覆盖「已结束运行的落盘与读回」，进程结束后记录不自动保留

当前定位：可复用的基础运行时内核，不是完整通用 Agent 平台。

## 许可

待定（对外发布前必须确定）。未确定前默认保留所有权利。

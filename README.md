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
| `RunStore` | 把终态运行结果按 run_id 原子落盘，并单独保存生命周期事件，支持查询运行中的状态 |

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

当前 v0.1 已包含线性图、条件路由、状态增量、Trace、工具适配、最大步数限制、协作式取消、节点边界超时和运行生命周期事件；不包含重试、并行、LLM 或多 Agent 调度。

v0.2 的第一步提供 `Checkpoint`、`CheckpointStore`、`InMemoryCheckpointStore` 和 `FileCheckpointStore` 快照协议。将 `checkpoint_store` 传给 `Runtime` 后，每次成功节点边界会保存状态、状态版本、下一节点和执行步数；`load_for()` 会校验快照所属的 `graph_id`。它们只保存可恢复状态，不自动恢复运行，也不承诺 exactly-once 执行。

恢复必须由调用方显式触发。将存储中的非终态 checkpoint 传给兼容的 `Runtime.resume()`，运行会从 `next_node` 继续，并沿用原来的 `run_id`、状态版本和累计步数：

```python
checkpoint = store.load_for("run-123", "graph")
if checkpoint is not None and checkpoint.next_node is not None:
    result = Runtime(graph_id="graph", checkpoint_store=store).resume(graph, checkpoint)
```

恢复不会重放 checkpoint 之前的节点，也不提供外部副作用的 exactly-once 保证；调用方仍需为工具调用设计幂等策略。

### 运行生命周期

Runtime 为每个运行维护以下状态：`PENDING`、`RUNNING`、`COMPLETED`、`BLOCKED`、`FAILED` 和 `CANCELED`。普通运行从 `PENDING` 进入 `RUNNING`，然后进入一个终态；终态不会被普通状态更新覆盖。显式 `resume()` 是从带有非终态 checkpoint 的失败运行重新进入 `RUNNING` 的专用入口。

```python
status = runtime.get_status(result.run_id)
events = runtime.get_lifecycle_events(result.run_id)
accepted = runtime.cancel(result.run_id)
```

需要跨 Runtime 实例查询状态时，构造器传入现有 `RunStore`：

```python
store = RunStore("runs")
runtime = Runtime(store=store)
```

`cancel()` 只发出协作式取消请求。Runtime 会在节点开始前或节点返回后确认请求，无法安全强制中断正在运行的同步 Python 函数。`timeout_seconds` 也只在节点边界生效；超时返回 `CANCELED`，原因码为 `TIMEOUT`，不会伪装成普通业务失败。终态结果及其生命周期事件会由 `RunStore` 一起保存。

### 公共 API

```python
from agent_core import Graph, NodeResult, NodeStatus, RunStore, Runtime

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
- 没有强制中断、重试、调用预算、幂等键
- 工具能力集合是**进程内逻辑约束**，不是操作系统沙箱或真正的身份鉴权
- 持久化覆盖节点边界 checkpoint 的落盘与读回，不负责跨进程自动调度或清理策略

当前定位：可复用的基础运行时内核，不是完整通用 Agent 平台。

## Agent 框架外壳

如果应用需要把模型、工具、记忆和工作流组合成一个可复用的 Agent，可以使用上层外壳。外壳不内置模型调用或规划器；这些能力通过 `ModelAdapter` 和节点函数注入，并继续使用同一套图执行、Trace、生命周期和持久化语义。

```python
from agent_core import AgentBuilder, NodeResult

agent = (
    AgentBuilder("text-agent", instructions="处理用户文本")
    .add_node("prepare", lambda ctx: NodeResult.success({"text": ctx.input["text"]}))
    .add_node("finish", lambda ctx: NodeResult.success({"done": True}))
    .set_entry("prepare")
    .add_edge("prepare", "finish")
    .build()
)
result = agent.run({"text": "hello"})
# result.status == RunStatus.COMPLETED
# result.state["done"] is True
```

Agent 节点收到的 `AgentNodeContext` 还包含 `model`、`tools`、`memory` 和 `cancel_requested`。`InMemoryMemory` 适合开发和测试；生产环境可实现 `MemoryStore` 接入自己的存储。`AgentResult.run` 保留底层 `RunResult`，因此不会丢失节点 Trace 或生命周期信息。`agent.describe()` 返回编译后的图拓扑（入口、节点、边），可用于 UI 展示和自省。

## 声明式 Agent 装配

`agent_core/nodes.py` 提供确定性节点工厂库，让 Agent 可以用 JSON 配置组装而不必写 Python:

| 节点类型 | 职责 |
|---|---|
| `validate_input` | 要求某输入字段为非空字符串并修剪，缺失即 BLOCKED（原因码可配） |
| `split_steps` | 按句子边界把文本字段拆解为有序计划条目 |
| `template` | 用 `{state_key}` 占位符渲染字符串到状态；只允许顶层键引用，缺失即 FAILED |
| `tool` | 调用注册工具，参数可为字面量或状态模板；BLOCKED/FAILED 原因码原样透传 |
| `route_by_value` | 按状态值选择出边路由，可配默认路由，无法解析时 BLOCKED |

```bash
python -m agent_core.server --config examples/support_agent.json
```

配置文件可以是单个 Agent 配置或 `{"agents": [...]}`；装配时校验（未知节点类型、重复 id、非法参数都会在启动期报错），应用可用 `register_node_factory()` 扩充节点类型。`examples/support_agent.json` 是完整示例。

## HTTP 服务与工作台

`agent_core/server.py` 用纯标准库把运行时暴露为 HTTP API，并托管 `frontend/` 工作台：

```bash
python -m agent_core.server --host 127.0.0.1 --port 8000 --store-dir outputs/server_runs
```

打开 `http://127.0.0.1:8000` 即得工作台：提交任务 → 实时生命周期时间线 → 节点级 Trace → 结果面板。服务不可达时页面自动进入离线演示模式，不会伪装成真实执行。

服务端行为与运行时语义一致：运行是同步状态机 + 线程外壳，不引入并行节点调度；`wait=false` 只是把执行移到后台线程，生命周期事件照常原子落盘。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 存活检查与已注册 Agent 列表 |
| `GET` | `/api/agents` | Agent 描述与图拓扑（`Agent.describe()`） |
| `POST` | `/api/runs` | 创建运行；`wait=true`（默认）同步返回终态，`wait=false` 返回 `202` 后轮询 |
| `GET` | `/api/runs` | 已持久化的运行列表与活跃运行 |
| `GET` | `/api/runs/{id}` | 终态结果（含 Trace 与生命周期事件） |
| `GET` | `/api/runs/{id}/status` | 当前生命周期状态（含异步运行的 `PENDING`/`RUNNING`） |
| `GET` | `/api/runs/{id}/events` | 生命周期事件序列 |
| `GET` | `/api/runs/{id}/events/stream` | SSE 实时推送生命周期事件，终态以 `done` 帧收尾 |
| `POST` | `/api/runs/{id}/cancel` | 发出协作式取消请求，返回是否受理 |

错误统一为 `{"error": {"code", "message"}}`：非法 JSON/输入返回 `400`，未知 Agent 或运行返回 `404`，重复占用 `run_id` 返回 `409`，路径穿越与不支持类型拒绝访问。静态文件只从 `frontend/` 目录解析。

`agent.run()` 是同步调用，异步模式由服务器线程承载；取消仍只能在节点边界生效，与 Runtime 的协作式取消语义一致。

## 许可

待定（对外发布前必须确定）。未确定前默认保留所有权利。

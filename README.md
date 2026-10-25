# agent-core

领域中立（domain-neutral）的 Agent 工作流运行时。2026-10-20 从 ICT 晶圆质检项目蒸馏而来：晶圆检测只是首个应用，不进入框架核心类型。

## 安装与启动

```bash
pip install -e .            # 安装到当前环境(零依赖,Python >= 3.10)
agent-core-server --port 8000   # 启动 HTTP 服务与工作台
```

开发时可直接 `python -m agent_core.server`;两者等价。

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

## 模型接入

`agent_core.models.OpenAICompatibleModel` 是内置的模型适配器,纯标准库实现,兼容所有 OpenAI 风格的 chat completions 端点(GLM、DeepSeek、OpenAI、vLLM、Ollama 等)。它每次调用恰好发一个 HTTP 请求、绝不重试;传输/协议失败抛 `ModelRequestError`(带稳定原因码,不含密钥或载荷)。

配置通过三个环境变量(全部齐备才生效,半配置视为未配置):

```bash
export AGENT_MODEL_BASE_URL=https://open.bigmodel.cn/api/paas/v4   # 任意 OpenAI 兼容端点
export AGENT_MODEL_API_KEY=your-key
export AGENT_MODEL_NAME=glm-4
agent-core-server --config examples/assistant_agent.json
```

声明式 `llm` 节点(配合 `examples/assistant_agent.json`):

| 参数 | 说明 |
|---|---|
| `prompt` | `{state_key}` 模板渲染的用户提示 |
| `system` | 可选系统提示模板 |
| `into` | 回复写入的状态键(默认 `reply`) |

fail-closed 语义:未绑定模型 → BLOCKED `MODEL_NOT_CONFIGURED`(不伪装);模板缺键 → FAILED `PROMPT_KEY_MISSING`;端点 HTTP 错误/超时/响应畸形 → FAILED 对应原因码。`GET /api/health` 如实报告 `model_configured`。适配器不解析提示词、不做 JSON mode 或函数调用;结构化输出应通过工具节点实现。

### 自训练 / 本地模型

`ModelAdapter` 是结构化协议,不要求继承、不绑定 HTTP——任何有 `complete(messages, *, tools, context) -> {"content": str}` 方法的对象都是合法适配器。三种接入路径:

1. **主流推理框架部署(vLLM/SGLang/Ollama/Xinference/LMDeploy 等)**:它们都暴露 OpenAI 兼容端点,直接用内置 `OpenAICompatibleModel`,零代码。
2. **自定义 HTTP 协议**:实现自己的 `complete`,内部用任意客户端调用你的服务,`model=` 传入。
3. **进程内直连(无 HTTP)**:`complete` 就是普通 Python 调用,直接包装你的模型对象;失败时抛携带 `reason_code` 属性的异常,节点会把它如实报告为 FAILED 原因码。

`examples/local_model.py` 是路径 3 的可运行示例(`python -m examples.local_model`),包含成功与失败契约两种形态;把它接到 HTTP 服务只需 `create_server(model=LocalTrainedModel())`。

### 视觉 / 领域模型(CXR-MARS 案例)

图像进、结构化概率出的模型(分类/检测/质检)**不是** ModelAdapter——那是对话形状的契约;正确的接入位置是**工具边界**:结构化参数进、结构化 JSON 出、能力显式授予、fail-closed。`examples/cxr_bridge.py` 演示把胸片多标签分析服务(CXR-MARS,`POST /api/predict`)接成一个 Agent 可委派的工具:

```python
tools = build_cxr_tools("http://127.0.0.1:8300")   # cxr_analyze 工具,要求 cxr_inference 能力
```

配套要点:声明式 `tool` 节点用 `capabilities` 参数显式授予能力(未声明 → BLOCKED `CAPABILITY_DENIED`,绝不隐式授予);工具抛出携带 `reason_code` 属性的异常时,该原因码会原样进入节点结果(领域失败不伪装、不被 `TOOL_EXCEPTION` 抹平);`python -m examples.cxr_bridge` 直接起一个挂载审阅 Agent 的完整服务。

## 声明式 Agent 装配

`agent_core/nodes.py` 提供确定性节点工厂库，让 Agent 可以用 JSON 配置组装而不必写 Python:

| 节点类型 | 职责 |
|---|---|
| `validate_input` | 要求某输入字段为非空字符串并修剪，缺失即 BLOCKED（原因码可配） |
| `split_steps` | 按句子边界把文本字段拆解为有序计划条目 |
| `template` | 用 `{state_key}` 占位符渲染字符串到状态；只允许顶层键引用，缺失即 FAILED |
| `tool` | 调用注册工具，参数可为字面量或状态模板；BLOCKED/FAILED 原因码原样透传 |
| `route_by_value` | 按状态值选择出边路由，可配默认路由，无法解析时 BLOCKED |
| `subagent` | 委派给另一个已注册 Agent 作为嵌套运行（独立 run_id 与 Trace）；子运行终态原样传播（BLOCKED/FAILED 保持 fail-closed），需要装配方提供 Agent 解析器 |

```bash
python -m agent_core.server --config examples/support_agent.json
```

配置文件可以是单个 Agent 配置或 `{"agents": [...]}`；装配时校验（未知节点类型、重复 id、非法参数都会在启动期报错），应用可用 `register_node_factory()` 扩充节点类型。`examples/support_agent.json` 是完整示例。`--config` 文件被修改时会自动热重载（默认 2 秒轮询）：新配置先完整构建校验，成功才原子替换注册表，失败保留旧版；从配置移除的 Agent 会被注销。配置之间的 `subagent` 依赖环在装载期拒绝。`examples/orchestrator_agent.json` 演示委派编排。

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
| `GET` | `/api/runs` | 运行记录列表：`?limit=`（1-100，默认 20）、`?offset=`、`?status=` 过滤；按开始时间倒序，每条含状态、原因码、摘要与 Agent 归属 |
| `GET` | `/api/runs/{id}` | 终态结果（含 Trace 与生命周期事件） |
| `GET` | `/api/runs/{id}/status` | 当前生命周期状态（含异步运行的 `PENDING`/`RUNNING`） |
| `GET` | `/api/runs/{id}/events` | 生命周期事件序列 |
| `GET` | `/api/runs/{id}/events/stream` | SSE 实时推送生命周期事件，终态以 `done` 帧收尾 |
| `GET` | `/api/sessions` | 会话列表 |
| `GET` | `/api/sessions/{id}` | 会话记忆快照 |
| `DELETE` | `/api/sessions/{id}` | 清除会话记忆 |
| `POST` | `/api/runs/{id}/cancel` | 发出协作式取消请求，返回是否受理 |
| `GET` | `/api/runs/{id}/checkpoint` | 检查点检查:恢复目标节点、状态、版本与步数 |
| `POST` | `/api/runs/{id}/resume` | 从非终态 checkpoint 显式恢复；可选 `state_updates` 注入状态修正 |

恢复语义与 Runtime 一致：只有 `BLOCKED`/`FAILED` 且存在未完成边界的运行可恢复；`state_updates` 合并进 checkpoint 状态并把 `state_version` 前移，恢复产生 `run.resumed` 生命周期事件。恢复后的 Trace 只覆盖恢复段。运行→Agent 的归属记录持久化在 `RunStore` 目录的 `.agent_index.json`(服务端簿记,不进入核心持久化模式),因此**服务重启后仍可恢复**;若索引不可读则回退为仅本进程可恢复。`examples/recovery_agent.json` 演示"中途阻断 → 注入修正 → 完成"的完整闭环。

### 会话记忆

`POST /api/runs` 可携带可选 `session_id`：服务端为每个会话维护一份 `InMemoryMemory`,以工厂方式构建绑定该记忆的 Agent 实例，同会话的多次运行共享上下文。工作台 Agent 会记录已处理任务并在摘要中标注"本会话第 N 个任务";运行负载中的 `session` 字段与 `GET /api/sessions/{id}` 返回记忆快照,`DELETE` 清除。会话仅存在于本进程,重启即清空;记忆是节点显式读写的,运行时本身不做隐式记忆。

错误统一为 `{"error": {"code", "message"}}`：非法 JSON/输入返回 `400`，未知 Agent 或运行返回 `404`，重复占用 `run_id` 返回 `409`，路径穿越与不支持类型拒绝访问。静态文件只从 `frontend/` 目录解析。

`agent.run()` 是同步调用，异步模式由服务器线程承载；取消仍只能在节点边界生效，与 Runtime 的协作式取消语义一致。

## 许可

待定（对外发布前必须确定）。未确定前默认保留所有权利。

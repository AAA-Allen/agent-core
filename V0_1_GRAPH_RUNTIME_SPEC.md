# agent-core v0.1 Graph Runtime API 与测试规范

状态：开发基线（接口冻结前评审稿）
日期：2026-10-23

## 1. 基线事实

当前仓库实际提供：

- `Step`、`StepOutcome`、`RunContext`、`WorkflowRunner`
- `Tool`、`ToolRegistry`、`tool_step`
- `TraceEvent`、`RunResult`、`RunStore`
- 57 个单元测试，覆盖线性执行、工具调用和结束态 JSON 持久化

当前仓库没有实现：

- `Graph`、`Node`、`Edge`、`NodeResult`
- `Checkpoint`、暂停恢复、重试、取消或并行调度
- `ModelAdapter` 或 `Replay`
- 完整的 `PolicyEngine`

本规范是目标接口，不把设计文档中的未实现能力视为现有能力。

## 2. v0.1 范围

v0.1 交付一个单进程、同步、单入口、非并行的有向图执行器。

必须支持：

- 线性节点执行
- 受控条件分支
- 显式状态增量和 Runtime 合并
- 静态图校验
- 节点成功、阻断、失败
- 循环和最大节点执行步数
- 统一执行标识和结构化 Trace
- 现有线性 `WorkflowRunner` 行为兼容

明确不支持：持久化恢复、`PAUSED`、人工审批、自动重试、超时、取消、并行、LLM、Memory、Planner、分布式调度和新的权限系统。

## 3. 公共 API

目标模块为 `agent_core.graph` 和 `agent_core.runtime`。具体文件位置可以调整，但公共名称和语义保持不变。

### 3.1 状态和上下文

```python
JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
State = dict[str, JSONValue]

@dataclass(frozen=True)
class NodeContext:
    run_id: str
    execution_id: str
    node_id: str
    state_version: int
    input: Mapping[str, JSONValue]
```

节点只能通过 `NodeContext` 读取输入和当前状态快照。节点不得修改共享状态、Runtime、Trace 或权限对象。

### 3.2 节点结果

```python
class NodeStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"

@dataclass(frozen=True)
class NodeResult:
    status: NodeStatus
    updates: Mapping[str, JSONValue] = field(default_factory=dict)
    route: str | None = None
    reason_code: str | None = None
    output: object | None = None
    simulated: bool = False

    @classmethod
    def success(cls, updates=None, *, route=None, output=None, simulated=False) -> "NodeResult": ...

    @classmethod
    def blocked(cls, reason_code: str) -> "NodeResult": ...

    @classmethod
    def failed(cls, reason_code: str) -> "NodeResult": ...
```

约束：

- `SUCCEEDED` 可以有 `updates`，不能携带 `reason_code`。
- `BLOCKED` 和 `FAILED` 必须有非空 `reason_code`。
- `BLOCKED`、`FAILED` 的 `updates` 不提交到 State。
- `route` 只对 `SUCCEEDED` 有效；其他状态携带 route 必须被拒绝。
- `updates` 必须是 JSON 可序列化对象，键必须是字符串。

### 3.3 图定义

```python
NodeCallable = Callable[[NodeContext], NodeResult]

@dataclass(frozen=True)
class Node:
    node_id: str
    execute: NodeCallable

@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    when: str | None = None

class Graph:
    def add_node(self, node_id: str, execute: NodeCallable) -> "Graph": ...
    def set_entry(self, node_id: str) -> "Graph": ...
    def add_edge(self, source: str, target: str, *, when: str | None = None) -> "Graph": ...
    def compile(self) -> "CompiledGraph": ...
```

`Graph` 是可变的构建器；`CompiledGraph` 是不可变的执行定义。`compile()` 不得调用节点、工具或模型。

静态校验必须拒绝：空图、重复节点、重复边、缺失入口、边端点不存在、不可达节点、空白标识、重复路由标签和非法自引用配置。

自环和一般循环在 v0.1 允许，但必须由执行步数上限保护。

### 3.4 Runtime

```python
class Runtime:
    def __init__(self, *, max_steps: int = 100): ...

    def run(
        self,
        graph: CompiledGraph,
        input_data: Mapping[str, JSONValue],
        *,
        run_id: str | None = None,
    ) -> RunResult: ...
```

`max_steps` 必须为正整数。每执行一个节点增加一次计数；超过上限时不再执行节点，返回 `FAILED` 和 `MAX_STEPS_EXCEEDED`。

v0.1 不提供 `resume()`、`cancel()` 或 `checkpoint_store` 参数。

### 3.5 结果和 Trace

```python
class RunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"

@dataclass(frozen=True)
class TraceEvent:
    event_id: str
    run_id: str
    node_id: str | None
    execution_id: str | None
    event_type: str
    state_version: int
    started_at: str
    duration_ms: float | None = None
    reason_code: str | None = None

@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: RunStatus
    state: Mapping[str, JSONValue]
    trace: tuple[TraceEvent, ...]
    stopped_at: str | None = None
    reason_code: str | None = None
```

`run_id` 在一次运行内唯一；每次节点执行生成唯一 `execution_id`；每次成功提交 State 后 `state_version` 加一。v0.1 的 Trace 至少产生：

```text
run.started
node.started
node.completed | node.blocked | node.failed
run.completed | run.blocked | run.failed
```

策略检查、Checkpoint 和恢复事件留给后续版本。Trace 只保存安全元数据，不保存密钥、完整凭据或未经脱敏的敏感内容。

## 4. 运行语义

### 4.1 节点执行和提交

每个节点按以下顺序处理：读取状态快照、生成执行标识、调用节点、验证 `NodeResult`、提交成功增量、产生 Trace、选择下一条边。

只有 `SUCCEEDED` 的 `updates` 才能合并到 State。v0.1 使用浅层键更新：增量中的键覆盖同名键，未出现的键保持不变。节点不能直接修改已有嵌套对象；需要整体替换该键的值。

状态提交失败时，当前 Run 立即 `FAILED`，原因码为 `STATE_COMMIT_FAILED`，不自动重试节点。

### 4.2 条件路由

节点通过 `route` 返回受控标签，Runtime 只做标签匹配，不执行字符串表达式。

- 没有出边：当前节点是终点，Run 为 `COMPLETED`。
- 有一条无标签边：只能在 `route is None` 时匹配。
- 有标签边：必须存在且只能存在一条同标签边。
- 零条匹配：Run 为 `FAILED`，原因码 `NO_ROUTE`。
- 多条匹配：Run 为 `FAILED`，原因码 `AMBIGUOUS_ROUTE`。
- `BLOCKED` 或 `FAILED` 节点不进行路由，也不执行后继节点。

同一节点的出边不得混用无标签默认边和多个可能匹配的标签边；这类歧义应在 `compile()` 阶段拒绝。

### 4.3 终态和错误码

稳定的 v0.1 原因码至少包括：

| 原因码 | 含义 |
|---|---|
| `NODE_EXCEPTION` | 节点抛出未处理异常 |
| `INVALID_NODE_RESULT` | 节点返回非法结果 |
| `INVALID_STATE_DELTA` | 增量不是合法 JSON 对象 |
| `NO_ROUTE` | 没有匹配的后继边 |
| `AMBIGUOUS_ROUTE` | 匹配到多条后继边 |
| `MAX_STEPS_EXCEEDED` | 超过最大节点执行步数 |
| `STATE_COMMIT_FAILED` | 状态提交失败 |
| `TOOL_NOT_FOUND` | 现有工具边界返回的稳定错误码 |
| `CAPABILITY_DENIED` | 现有工具能力检查拒绝 |
| `TOOL_EXCEPTION` | 现有工具调用异常 |

`BLOCKED` 是 v0.1 终态，不表示可恢复暂停。人工审批和恢复语义在 v0.2 单独定义。

## 5. 现有 API 兼容策略

- 保留 `Step`、`StepOutcome`、`WorkflowRunner` 和现有导出名称。
- 现有线性 `WorkflowRunner` 测试必须继续通过。
- 图执行器可以内部把线性节点转换为图节点，但不能维护两套互相矛盾的状态语义。
- `tool_step` 继续使用 `ToolRegistry` 的现有能力检查；v0.1 不引入新的 `PolicyEngine`，也不能绕过 `ToolRegistry.invoke()`。
- `RunStore` 仍只负责已结束运行的保存与读取，不冒充 CheckpointStore。
- 不新增 `ModelAdapter`、`Replay` 或恢复 API，除非有对应实现和测试。

## 6. 验收测试规范

新增测试应放在 `tests/`，并与现有测试一起运行。

### 图定义

- 空图无法编译。
- 重复节点、重复边、缺失入口和非法端点无法编译。
- 不可达节点无法编译。
- 节点标识和路由标签不能为空白。
- 编译不会调用任何节点函数。

### 执行

- 线性图按入口和边顺序执行并完成。
- 成功节点的 updates 合并到下一节点可见的 State。
- 节点不能通过修改传入 State 影响 Runtime 内部状态。
- BLOCKED 节点停止运行，后继节点不执行，最终保留原因码。
- FAILED 节点停止运行，后继节点不执行，最终保留原因码。
- 节点抛异常返回 `NODE_EXCEPTION`，不泄露异常消息到 Trace。
- 非法 NodeResult 返回 `INVALID_NODE_RESULT`。
- 非法 updates 不提交 State。

### 路由和循环

- 正确标签只执行对应后继。
- 零匹配返回 `NO_ROUTE`。
- 多匹配返回 `AMBIGUOUS_ROUTE`。
- 无出边的节点正常结束。
- 循环在 `max_steps` 内执行。
- 超过 `max_steps` 返回 `MAX_STEPS_EXCEEDED`，不会额外执行节点。

### 标识和 Trace

- 自动生成的 `run_id` 唯一且格式稳定。
- 每个节点执行都有唯一 `execution_id`。
- Trace 的 `run_id`、`node_id`、`execution_id` 和 `state_version` 可关联。
- State 每次成功提交后版本递增；失败和阻断不提交 updates。
- 真实执行和模拟预览如果未来加入，必须有不同模式和不同结果标识；模拟不能产生真实 `COMPLETED` 结果。

### 工具和兼容性

- 现有 57 个测试全部继续通过。
- 图节点通过 `tool_step` 调用工具时，`TOOL_NOT_FOUND`、`CAPABILITY_DENIED` 和 `TOOL_EXCEPTION` 语义不变。
- 工具被能力拒绝时，工具函数本身不会执行。
- 不存在的 `ModelAdapter`、`Replay`、`Checkpoint` 不应出现在 v0.1 的公共导出中。

## 7. 实施顺序

1. 先将本规范中的公开类型和不变量转为测试。
2. 实现 `Graph`、`Edge` 和 `compile()` 静态校验。
3. 实现单线程 `Runtime`，复用现有 JSON 快照和异常收敛逻辑。
4. 增加线性图适配测试，证明旧线性行为没有退化。
5. 更新 README，明确已实现 API 与未来设计的边界。
6. 测试全部通过后，再冻结 v0.1 版本，不在同一轮加入 Checkpoint、模型或多 Agent。

v0.1 的完成标准是：图定义可验证、状态更新可预测、分支和循环有确定语义、失败边界可审计、现有 API 不回归。持久化恢复和模型生态属于后续版本。

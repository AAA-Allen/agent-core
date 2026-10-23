# v0.1 文档与仓库校准记录（历史快照）

日期：2026-10-23

说明：本文记录 Graph Runtime 实现前的校准状态。当前仓库已经完成 v0.1 Graph Runtime，并增加 v0.2 Checkpoint 与显式恢复；本文中的实现前描述不再代表当前代码状态。

## 1. 事实状态

当前仓库的实现仍然是线性 Runtime：

- `agent_core/runtime.py`：`Step`、`StepOutcome`、`RunContext`、`WorkflowRunner`
- `agent_core/tools.py`：`Tool`、`ToolRegistry`、`tool_step`
- `agent_core/persistence.py`：结束态 JSON `RunStore`
- `tests/`：现有线性、工具和持久化测试

当前没有 `Graph`、`NodeResult`、`Runtime`、`Checkpoint`、`ModelAdapter` 或 `Replay` 的实现。

`agent_runtime_document_pack` 已在工作区，但其中的 `test_runtime_contract.py` 是目标契约模板，当前不能直接运行。它依赖不存在的 `agent_core.graph`、`agent_core.runtime.Runtime` 和 `agent_core.results.NodeResult`。

## 2. 文档之间的接口冲突

| 项目 | 文档包 | 先前 v0.1 草案 | 建议冻结值 |
|---|---|---|---|
| 执行器 | `Runtime` | `GraphRuntime` | `Runtime` |
| 节点成功状态 | `OK` | `SUCCEEDED` | `SUCCEEDED` 或 `OK` 二选一，推荐 `SUCCEEDED` |
| 状态增量字段 | `updates` | `delta` | `updates`，与文档包测试模板一致 |
| 边条件字段 | `when` | `label` | `when`，但值必须是受控路由标签 |
| 节点函数签名 | `fn(ctx)` | `fn(state, ctx)` | `fn(ctx)`，Context 提供只读 state |
| 节点结果模块 | `agent_core.results` | 未固定 | `agent_core.results` |
| 图模块 | `agent_core.graph` | `agent_core.graph` | `agent_core.graph` |
| 模拟结果 | `simulated=True` 且可完成 | 未纳入核心 API | `RunResult.simulated` 必须保留，不能伪装为真实执行 |

实现前必须消除这些冲突。否则测试名称、字段和公共导入会不断变动，无法形成稳定 v0.1。

## 3. 建议采用的最终 API

以用户提供的文档包为产品契约，采用以下外部接口：

```python
from agent_core.graph import Graph
from agent_core.runtime import Runtime
from agent_core.results import NodeResult

graph = (
    Graph()
    .add_node("validate", validate)
    .add_node("analyze", analyze)
    .set_entry("validate")
    .add_edge("validate", "analyze", when="valid")
    .compile()
)

result = Runtime(max_steps=100).run(graph, input={"text": "hello"})
```

建议的节点结果字段：

```python
NodeResult(
    status=NodeStatus.SUCCEEDED,
    updates={"key": "value"},
    route="valid",
    output=None,
    reason_code=None,
    simulated=False,
)
```

这里保留文档包中的 `updates`、`output` 和 `simulated`，但将状态从自由字符串收敛为 `NodeStatus` 枚举。这样既保留模板意图，也避免 `"OK"`、`"SUCCESS"`、`"SUCCEEDED"` 并存。

节点函数统一采用：

```python
def node(ctx: NodeContext) -> NodeResult:
    ...
```

`NodeContext` 提供只读 `input`、`state`、`run_id`、`node_id`、`execution_id` 和 `state_version`。

## 4. 文档包测试模板需要的校准

在实现前需要调整 `test_runtime_contract.py`：

1. `from agent_core.runtime import Runtime` 只有在新增 `Runtime` 后才成立。
2. `from agent_core.results import NodeResult` 需要新增 `results.py`。
3. `status="OK"` 改为 `NodeStatus.SUCCEEDED`，或明确冻结字符串 `OK`；推荐枚举。
4. `add_edge(..., when=...)` 要和图实现一致，并规定无条件边的匹配规则。
5. 模拟测试需要明确：允许 Run `COMPLETED`，但 `result.simulated is True`；任何需要真实执行证据的后继节点必须能拒绝模拟状态。
6. 增加状态合并、零路由、多路由、不可达节点、Trace `execution_id` 和 `state_version` 测试。
7. 增加现有 `WorkflowRunner` 的兼容性测试，不把旧测试替换掉。

## 5. 与当前代码的迁移边界

- 保留当前 `WorkflowRunner`、`Step`、`StepOutcome` 的公开接口和原有测试。
- 新增 Graph Runtime，先不要重命名或删除线性 API。
- 新 Graph Runtime 可内部复用 JSON 快照、异常收敛和工具调用逻辑，但不能直接复用 `_input`、`_outputs` 这类可绕过只读约束的内部字段。
- `tool_step` 仍必须通过 `ToolRegistry.invoke()`，Graph 节点不得直接绕过现有工具边界。
- `RunStore` 仍是结束态记录，不应在 v0.1 改名为 `CheckpointStore` 或声称支持恢复。

## 6. 当前结论

文档包可以作为 v0.1 产品和工程基线，但在实现前应先冻结上表中的公共名称和字段。建议采用“文档包的模块/API 命名 + 严格枚举和显式状态语义”的组合，然后把模板测试校准为真实包路径，再开始写 Graph Runtime。

当前不能直接运行文档包的 pytest 模板；全局 pytest 还会被环境中的 `langsmith` 插件和缺失的 `annotated_types` 依赖阻断。现有项目测试可用 `python -m unittest discover -s tests -q` 运行。

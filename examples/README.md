# 示例应用：文本处理管线

用与质检完全无关的领域（纯文本处理）跑通 `agent_core`，证明框架核心不含任何业务词汇——同一运行时既跑晶圆质检工作流，也跑这个文本管线。

## 跑法

```bash
python examples/text_pipeline.py
# 或
python -m examples.text_pipeline
```

Graph Runtime 示例：

```bash
python -m examples.graph_pipeline
```

## 流程

```
normalize（工具）→ validate（自定义步骤）→ analyze（工具）→ classify（工具）
```

- 工具经 `ToolRegistry` 注册、`tool_step` 绑定，与质检工作流同一条 Trace
- 空文本在 `validate` 处 **BLOCKED**（原因码 `EMPTY_TEXT`），后续步骤不执行——与质检工作流相同的 fail-closed 决策边界
- 运行记录落盘 `outputs/examples/text_runs/`（`RunStore`，与质检入口同一机制）

## 行为保护

`tests/test_examples.py` 覆盖完成路径、阻断路径与 Trace 完整性，示例改坏会被测试抓住。

`graph_pipeline.py` 展示 v0.1 的 `Graph`、条件路由、状态更新和 `BLOCKED` 终态。

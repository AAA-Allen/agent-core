# agent_lite

`agent_lite` 是从 `agent_core` 项目中独立提取的轻量 Agent 外壳。它不修改或依赖原来的 `agent_core`，只有一个核心循环：对话、模型调用、工具调用和历史消息。

## 最小用法

```python
from agent_lite import Agent

agent = Agent(model, system="你是一个简洁的助手")
print(agent.run("你好"))
```

模型只需要实现：

```python
complete(messages) -> {"content": "..."}
```

如果模型还实现 `complete_with_tools(messages, tool_schemas)`，Agent 会自动启用工具循环。

```python
from agent_lite import Agent, Tool

agent = Agent(model, tools=[Tool(
    "add", lambda a, b: a + b,
    description="计算两个数的和",
    parameters={"type": "object", "properties": {
        "a": {"type": "number"}, "b": {"type": "number"},
    }, "required": ["a", "b"]},
)])
```

原项目仍通过 `agent_core` 使用；这个包是并列的轻量入口。


# 更新日志

本项目遵循语义化版本。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.3.0] - 2026-10-25

模型接入、领域工具桥接与工程硬化:从确定性编排平台到可接真实智能的运行时。

### 新增

- **单实例约束**:存储目录级 OS 文件锁(flock/msvcrt,进程死亡自动释放),拒绝同目录第二服务实例;真实子进程硬杀 e2e 测试验证锁自动释放与记录完好。
- **流式输出**:适配器可选 `complete_stream(messages, on_delta)` 约定(内置适配器已实现,标准库 SSE 解析);`llm` 节点优先流式、无则回退 `complete`;进程内 `TokenBus`(历史回放 + 容量上限)桥接节点与 SSE;`/events/stream` 新增 `token` 帧,工作台实时渲染。
- **模型接入**:`agent_core.models.OpenAICompatibleModel`——纯标准库实现的 OpenAI 兼容 chat completions 适配器(单请求、不重试、失败带稳定原因码且不泄露密钥/载荷);`AGENT_MODEL_*` 环境变量配置(半配置视为未配置);声明式 `llm` 节点(模板提示 + fail-closed:未配置 BLOCKED、传输失败 FAILED 带原因码);服务端模型注入与 `/api/health` 的 `model_configured` 报告;`examples/assistant_agent.json`;本地假端点契约测试(不触网)。
- **领域工具桥接**(`examples/cxr_bridge.py`):视觉/领域模型经 Tool 边界接入(能力门控 + 原因码透传);声明式 `tool` 节点 `capabilities` 参数显式授予权限;`--allow-root` 路径白名单。
- **请求来源防护**:POST/DELETE 校验 Origin(localhost CSRF)与 Host(DNS rebinding),跨站请求 403;非浏览器客户端不受影响。
- **结构化运行冲突**:`RunConflictError(reason_code)` 取代消息文本约定(`RUN_ALREADY_ACTIVE`/`RUN_ID_ALREADY_USED`/`CHECKPOINT_INCOMPATIBLE`),服务端表驱动映射。

### 变更(0.x 破坏性)

- `ModelAdapter.complete` 移除未实现的 `tools`/`context` 参数,签名收敛为 `complete(messages)`;接口不再声明未交付的能力。
- `RunResult.stopped_at` 更名为 `stopped_at_node`(语义即节点名);旧名保留为只读弃用别名,持久化写新键、读兼容旧记录的 `"stopped_at"` 键,HTTP 负载键同步更名。

## [0.2.0] - 2026-10-25

在 0.1.x 图运行时之上新增完整的 Agent 平台层。核心运行时语义不变,全部新增能力保持确定性、fail-closed 与可审计边界。

### 新增

- **Agent 框架外壳**:`AgentBuilder`/`Agent`/`AgentSpec`,模型、工具、记忆通过协议注入;`AgentNodeContext.cancel_requested` 为活回调,支持节点内协作取消;`AgentResult` 保留完整运行时结果;`Agent.describe()` 暴露图拓扑。
- **声明式 Agent 装配**:`agent_core.nodes` 确定性节点工厂(`validate_input`/`split_steps`/`template`/`tool`/`route_by_value`/`subagent`),JSON 配置装配,装配期校验;`register_node_factory()` 扩展;`--config` 启动装载与 mtime 热重载(先校验后原子替换,失败保留旧版)。
- **子 Agent 编排**:`subagent` 节点把工作委派给其他注册 Agent 作为嵌套运行(独立 run_id/Trace/生命周期),终态原样传播;配置间依赖环装载期拒绝;委派共享会话记忆。
- **HTTP 服务**(`agent_core/server.py`,纯标准库):同步与异步(`wait=false`)运行、运行查询、生命周期事件、协作式取消、检查点检查与显式恢复(`state_updates` 状态修正)、SSE 实时事件流、会话记忆(创建/查看/清除)、运行列表分页/过滤与元数据缓存;运行→Agent 归属索引持久化,跨进程恢复。
- **工作台前端**:实时生命周期时间线、节点级 Trace 检查器(Trace/生命周期表格、状态 JSON)、运行中取消、可恢复运行的状态编辑对话框、嵌套运行钻取、服务端运行日志分页、Agent 拓扑展示、会话记忆清除;服务不可达时自动降级为离线演示模式。
- **会话记忆**:每会话 `InMemoryMemory`,`MemoryStore` 协议落地,同会话运行共享上下文。
- **打包**:`pip install -e .` / wheel,`agent-core-server` 控制台入口,版本单一来源化。
- **CI**:GitHub Actions 矩阵(Windows/Ubuntu × Python 3.10/3.13),unittest + pytest 双跑。

### 测试

- 测试规模从 170 增长到 244+:Agent 外壳、HTTP API、节点库、文档包契约校准(`tests/test_contract.py`)、恢复/会话/编排/热重载全套回归。

### 边界(仍未实现)

- 无 LLM/规划器内置、无并行节点调度、无重试、无强制中断、无分布式锁、无 exactly-once 外部副作用保证。
- 会话记忆仅存于进程内;身份鉴权未提供,服务默认仅绑定本机。

## [0.1.1] - 2026-10-24

- v0.1 图运行时:图定义与编译校验、条件路由、状态增量、Trace、协作式取消、节点边界超时、生命周期事件与持久化、Checkpoint/恢复、并发与边界加固系列。

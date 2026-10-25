# Night Development Progress

主线：运行生命周期状态机

## Baseline

- 实际测试基线：170 passed（Runtime 同 run_id 并发启动竞态加固后）
- 执行模型：同步、单进程、节点顺序执行
- 现有 `RunStore`：保存完成后的 `RunResult`
- 现有 `CheckpointStore`：保存节点边界快照，显式 `Runtime.resume()`

## Subtasks

- [x] A. 现状检查和生命周期设计
- [x] B. 状态模型与合法转换
- [x] C. Runtime 生命周期集成
- [x] D. 运行状态查询
- [x] E. 取消语义
- [x] F. 超时语义
- [x] G. 生命周期事件与持久化
- [x] H. 并发边界与错误处理
- [x] I. 全量回归、兼容性检查和文档更新

## Latest Verification

最近验证：`python -m unittest discover -s tests -q` 和禁用外部插件的 `pytest -q` 均为 170 passed。

## Known Limits

- 生命周期事件同时保存在 Runtime 内存和 RunStore 事件日志中。
- 同步 Python 节点不能被安全强制中断；取消和超时需要协作式边界。
- 取消请求在节点开始前或节点返回后确认；节点内部可通过 `ctx.cancel_requested` 协作检查。
- `timeout_seconds` 在节点边界确认，超时返回 `CANCELED`/`TIMEOUT`；无法强制中断同步节点。
- `RunStore` 保存终态 `RunResult` 及其生命周期事件；旧 JSON 记录缺少事件字段时按空事件兼容。
- `Runtime(store=...)` 会自动保存终态结果；新 Runtime 实例可从 RunStore 查询终态和生命周期事件。
- RunStore 另外原子保存 `<run_id>.events.json`，支持运行中的状态和事件查询；事件日志不计入 `list_runs()` 的运行结果列表。
- 跨 Runtime 实例 `resume()` 会从已持久化事件重建历史，并追加 `run.resumed`，不会覆盖旧终态事件。
- 持久化的 `RUNNING` 记录可通过显式 `resume()` 进入 `run.recovered`，用于崩溃后的边界恢复。
- 持久化事件重建会校验来源状态、目标状态和恢复转换，拒绝伪造或损坏的状态历史。
- Store 写入失败会向调用方抛出异常，不会伪装成运行成功或静默丢失记录；内存生命周期仍保留真实终态。
- Store 写入失败也会释放活动运行占用和取消请求，避免后续查询误报执行中。
- 仅支持外部线程发出协作式取消请求；不提供并行节点调度或通用线程安全承诺。
- 同一 `run_id` 的活动执行受到占用保护，重复 `resume()` 会被拒绝；该保护不等同于分布式锁。
- 普通 `run()` 不会覆盖已有持久化结果或生命周期；恢复必须通过显式 `resume()`。
- `RunStore` 读取内嵌或独立生命周期事件时都会重放状态转换并校验结果状态一致性。
- 生命周期事件类型必须与状态转换一致，例如 `run.completed` 只能表示进入 `COMPLETED`。
- 生命周期事件写入拒绝空序列，避免生成无法重放的事件记录。
- Checkpoint 在相同状态版本和步数下拒绝不同快照，避免无进展覆盖恢复点。
- 文件 Checkpoint 原子写入失败时清理临时文件，避免存储目录遗留无效记录。
- RunStore 结果和生命周期事件原子写入失败时清理临时文件。
- RunStore 写入生命周期事件前重放并校验完整状态转换。
- WorkflowRunner 仅提交 COMPLETED 步骤的输出；BLOCKED/FAILED 输出不进入最终 outputs。
- RunStore 结果和生命周期日志写入 schema_version=1；读取兼容缺失版本的旧记录并拒绝未知版本。
- RunContext 的顶层输入和输出快照不可直接修改，WorkflowRunner 通过内部提交入口更新已完成输出。
- RunStore 读取时校验结果 ID、outputs/state 映射类型和 Trace 的 run_id 归属。
- RunStore 读取时对 outputs/state 执行完整 JSON 快照校验，拒绝嵌套非法值。
- RunStore 写入时校验生命周期历史与 RunResult 状态一致，拒绝生成不可读记录。
- RunStore Trace 校验允许运行级 `run.*` 事件使用空 step，同时约束节点事件的 step 和耗时。
- RunStore Trace 校验 metadata 为对象，state_version 为非负整数。
- 生命周期事件重放校验 event_id 非空且全序列唯一。
- 生命周期事件重放校验 occurred_at 非空。
- RunStore 在同一进程内串行化写入，避免线程间同时写入记录。
- RunContext 内部输入快照递归只读，公开属性仍返回脱离内部状态的 JSON 副本。
- Graph NodeContext 的 input/state 私有快照递归只读，公开属性返回隔离副本。
- RunContext 和 NodeContext 共享递归只读/解冻工具，NodeContext 校验运行身份、状态版本和取消回调。
- RunResult 校验运行身份、状态、输出映射和模拟标记，并隔离 outputs/state/Trace 容器。
- TraceEvent 校验运行身份、步骤、时间、耗时、状态版本、原因码和 metadata，并隔离 metadata。
- 生命周期事件重放校验状态字段为 LifecycleStatus，原因码格式合法。
- Graph 的 Node/Edge 直接构造校验字段，CompiledGraph 保持节点定义不可变快照。
- NodeResult 深拷贝 updates/output，ToolResult 校验状态字段并隔离 output，Checkpoint 覆盖嵌套状态快照。
- 内存和文件 Checkpoint 存储复用单调性校验，并在进程内保护并发写入。
- RunStore 读写、FileCheckpointStore 读删和 ToolRegistry 注册/查找均有进程内并发边界保护。
- ToolResult 固化 COMPLETED/BLOCKED/FAILED 三态原因码和 error_type 规则，并隔离嵌套 output。
- Runtime 在生命周期初始化前占用活动 run_id，阻止同一 run_id 的并发启动竞态。
- Runtime 生命周期缓存的初始化、查询和取消访问通过独立 RLock 串行化，持久化调用不在锁内执行。
- 生命周期事件 occurred_at 必须是可解析的 ISO-8601 时间。
- 生命周期事件日志写入 schema_version=1，并有落盘形状测试。

## Acceptance

- 生命周期状态机已接入 `Runtime.run()` 和 `Runtime.resume()`。
- 六种状态、合法转换、终态保护、查询、取消、超时和生命周期事件持久化均有测试。
- 未实现：强制中断、重试、并行调度、分布式锁和 exactly-once 外部副作用保证。

## Post-I Hardening

- [x] 同一 `run_id` 的活动执行占用保护，重复 `resume()` 拒绝。
- [x] 终态事件或结果持久化失败时释放活动占用和取消请求。
- [x] 运行中事件日志可被新 Runtime 查询，持久化 `RUNNING` 可显式恢复。
- [x] 内存和文件 Checkpoint 存储拒绝无进展的冲突快照。
- [x] 文件 Checkpoint 替换失败时清理临时文件。
- [x] RunStore 结果和生命周期事件替换失败时清理临时文件。
- [x] RunStore 生命周期事件写入前拒绝非法状态转换。
- [x] WorkflowRunner 阻断和失败输出语义已固化并有回归测试。
- [x] RunStore 结果和生命周期日志 schema 版本校验已覆盖。
- [x] RunContext 顶层只读边界和延迟 JSON 校验行为已覆盖。
- [x] RunStore 损坏结果结构和 Trace 归属校验已覆盖。
- [x] RunStore outputs/state 嵌套 JSON 值校验已覆盖。
- [x] RunStore 写入生命周期历史与结果状态一致性已覆盖。
- [x] RunStore Trace 运行级事件与节点事件边界校验已覆盖。
- [x] RunStore Trace metadata 和 state_version 校验已覆盖。
- [x] 生命周期事件 event_id 唯一性校验已覆盖。
- [x] 生命周期事件 occurred_at 和 RunStore 进程内并发写入校验已覆盖。
- [x] 生命周期事件时间格式和事件日志 schema 形状已覆盖。
- [x] RunContext 嵌套输入递归只读和快照隔离已覆盖。
- [x] Graph NodeContext 嵌套 input/state 只读和快照隔离已覆盖。
- [x] 上下文只读工具统一和 NodeContext 参数边界校验已覆盖。
- [x] RunResult 字段校验和输入映射快照隔离已覆盖。
- [x] TraceEvent 字段边界和 metadata 快照隔离已覆盖。
- [x] 生命周期事件状态枚举和原因码边界已覆盖。
- [x] Graph Node/Edge 构造校验和 CompiledGraph 快照隔离已覆盖。
- [x] NodeResult、ToolResult 和 Checkpoint 嵌套快照边界已覆盖。
- [x] Checkpoint 存储并发写入和单调性保护已覆盖。
- [x] 持久化读写和 ToolRegistry 并发访问边界已覆盖。
- [x] ToolResult 三态字段语义和嵌套 output 隔离已覆盖。
- [x] 同一 run_id 并发启动只允许一个 Runtime 执行实例。
- [x] Runtime 生命周期缓存并发访问边界已覆盖。
- 当前工作区已提交；未跟踪的 `agent_runtime_document_pack/` 按约定保持不变。

## Session 2026-10-25:Agent 外壳测试、HTTP 服务与工作台

主线:让运行时可被真实应用使用——Agent 外壳补齐契约测试,新增 HTTP 服务与前端工作台。

### 已完成

- [x] `agent_core/agent.py` Agent 外壳 17 项契约测试(构造校验、依赖注入、内存快照隔离、阻断语义、协作式取消、RunStore 持久化、`describe()` 图拓扑)。
- [x] `AgentResult` 补充 `reason_code`/`stopped_at` 透传;`Agent.describe()` 暴露编译图拓扑。
- [x] `agent_core/server.py`:纯标准库 `ThreadingHTTPServer` 实现 HTTP API(健康检查、Agent 自省、同步/异步运行、结果/状态/事件查询、协作式取消)与 `frontend/` 静态托管。
- [x] 异步模式:POST `/api/runs` `wait=false` 返回 202,后台线程执行,状态端点回 `PENDING`(领取前)→ 真实生命周期 → 终态;活跃 `run_id` 冲突返回 409。
- [x] 错误契约:非法 JSON/输入 400、未知 Agent/运行 404、`run_id` 冲突 409、请求体超限 413、路径穿越拒绝;错误统一 `{"error": {code, message}}`。
- [x] `frontend/app.js` 重写:连接探测(在线/离线演示双模式)、异步提交 + 轮询、实时生命周期时间线、节点级 Trace 展示、运行中取消按钮、localStorage 历史持久化与恢复、详情面板(状态/原因码/执行路径/最终状态 JSON)。
- [x] 服务端 14 项测试(含异步完成、run_id 复用拒绝、活跃冲突、异步协作取消、静态托管与穿越防护)。
- [x] 端到端验证:curl 全端点 + 内置浏览器 GUI 黑盒(提交 → 实时时间线 → 完成 → 历史恢复),截图确认视觉状态。

### 修复

- 异步状态查询顺序:先查 Runtime 真实生命周期、仅在领取前回 `PENDING`,避免活跃集合掩盖 RUNNING 状态。
- 前端 `runLiveTask` 换 `run_id` 后同步更新 `state.activeId`,修复完成后界面不刷新的问题。

### Latest Verification

`python -m unittest discover -s tests -q` 201 passed(170 基线 + Agent 外壳 18 + HTTP 服务 13)。

### Known Limits

- 运行仍为同步状态机;异步模式只是服务器线程外壳,无并行节点调度、无重试、无强制中断。
- 取消只在节点边界生效;演示 Agent 节点执行毫秒级完成,取消窗口主要面向长耗时自定义节点。
- `RunStore` 默认目录 `outputs/server_runs`(已 gitignore);服务器默认绑定 127.0.0.1,未做身份鉴权,仅限本机使用。
- 前端历史存于浏览器 localStorage(最近 30 条);服务端运行记录以 `RunStore` 落盘为准。
- `agent_runtime_document_pack/` 按约定保持未跟踪。

## Session 2026-10-25(续):声明式 Agent 装配与 SSE 实时流

主线:从"能用"到"平台化、实时化"——Agent 不必写 Python 即可装配,运行过程毫秒级可见。

### 已完成

- [x] `agent_core/nodes.py` 确定性节点工厂库:`validate_input` / `split_steps` / `template` / `tool` / `route_by_value`;参数在装配期校验,运行期数据问题 fail-closed(BLOCKED/FAILED 带原因码);`template` 只允许顶层状态键占位,拒绝属性访问路径。
- [x] `build_agent_from_config()` + 服务端 `--config`:启动期从 JSON(单配置或 `agents` 列表)装载 Agent,共享 Runtime 与 RunStore;`register_node_factory()` 供应用扩充节点类型,重复注册拒绝。
- [x] `examples/support_agent.json`:完全由节点库装配的示例 Agent,作为契约测试用例常驻测试。
- [x] SSE 端点 `GET /api/runs/{id}/events/stream`:回放已持久化事件 → 增量推送 → `done` 帧携带终态;心跳注释保活,断连与超时有界(15 分钟上限),未知运行返回 JSON 404。
- [x] 前端 `watchLiveTask`:EventSource 消费 lifecycle 帧 + 去重,`done` 触发结果拉取;2 秒状态轮询看门狗兜底断连,保留离线演示模式。
- [x] 工作台运行检查器:日志视图读取服务端持久化运行(最近 12 条,可点击),详情弹窗展示完整 Trace 表格、生命周期迁移表格与最终状态 JSON;设置页展示各 Agent 图拓扑(来自 `describe()`)。
- [x] checkpoint 恢复 HTTP 入口:服务端默认挂 `FileCheckpointStore`,运行创建时记录 run→agent 归属;`POST /api/runs/{id}/resume` 校验可恢复性(BLOCKED/FAILED + 未完成边界),可选 `state_updates` 合并状态并把 state_version 前移;`resumable` 标志随运行负载返回。
- [x] 前端恢复闭环:Agent 选择器、恢复按钮(可恢复的终态运行可见)、prompt 输入状态修正 JSON、恢复结果直接渲染(再阻断时按钮保留)。
- [x] 修复:`clockTime` 对非法日期回退为 `--:--`,停止节点字段改用 trace 末事件时间,详情面板"停止于"直接展示 `stopped_at` 语义。
- [x] 服务端 21 项测试(新增恢复套件:状态修正恢复、空修正再阻断且保持可恢复、COMPLETED/无检查点拒绝、请求校验)。
- [x] 会话记忆:`SessionManager` 维护每会话 `InMemoryMemory`;注册表工厂化(`register_factory`/`build`),创建运行时按会话绑定记忆构建 Agent 实例;`POST /api/runs` 携带 `session_id`,运行负载与 `GET /api/runs/{id}` 返回记忆快照;`GET/DELETE /api/sessions/{id}` 查看/清除。工作台 Agent 用记忆记录已处理任务并在摘要标注会话序号;`InMemoryMemory.snapshot()` 支持内省。前端自动生成并持久化会话 ID,详情弹窗展示会话记忆,设置页可清除;Agent 下拉默认优先 workbench 并记住选择。
- [x] 测试:节点库 12 项(含示例文件端到端、自定义工厂注册)、服务端 17 项(含 SSE 推流、SSE 404、配置装载与非法配置拒绝)。
- [x] 端到端:curl 验证配置 Agent 装载与 SSE 回放;内置浏览器验证 EventSource 全流程。

### Latest Verification

`python -m unittest discover -s tests -q` 225 passed(170 基线 + Agent 外壳 19 + HTTP 服务 24 + 节点库 12)。

### Known Limits

- 节点库刻意不含表达式求值、网络调用或代码加载;动态行为必须经 `tool` 节点与注册工具进入。
- SSE 流按 150ms 轮询 Runtime 生命周期实现,不改变"事件在转换时刻落盘"的事实;跨进程推送仍以文件为准。
- 模板占位符是安全子集(无格式规格、无属性访问),复杂渲染应交给工具节点。
- 前端 localStorage 历史与 SSE 均假设单用户本机场景。
- 终态在 Runtime 内存先于结果落盘可见;服务端 `GET /api/runs/{id}` 对已知终态运行做有界等待(最多约 1 秒)桥接该窗口,前端结果读取对短暂 404 做有限重试。
- `AgentNodeContext.cancel_requested` 是活回调,节点内每次访问实时求值;长任务应轮询该标志实现协作退出。

## Session 2026-10-25(续二):契约测试校准与打包

### 已完成

- [x] `tests/test_contract.py`:文档包 `test_runtime_contract.py` 目标契约一对一校准(10 项)——顺序执行、条件路由、阻断/失败收敛、异常不泄露消息、编译期校验、max_steps、simulated 标记、Trace 归属与有序性。适配点:目标 API 的 `status="OK"` 对应实现的 `NodeResult.success()`(三态枚举);异常契约额外钉住"Trace 只含 error_type 不含消息"。
- [x] 校准抓到并修复真实缺口:图 Runtime 节点异常未记录 `error_type`(线性 WorkflowRunner 有)——`emit()` 增加 error_type 透传,`NODE_EXCEPTION` 事件带异常类型。
- [x] 打包:pyproject 0.2.0,`[project.scripts]` 提供 `agent-core-server` 控制台入口;`pip install -e .` 验证通过,元数据版本与命令可用。
- [x] 文档包契约模板保持未跟踪原样;校准结果以本文件与本仓库测试为准。

### Latest Verification

`python -m unittest discover -s tests -q` 235 passed(170 基线 + Agent 外壳 19 + HTTP 服务 24 + 节点库 12 + 契约 10)。

## Session 2026-10-25(续三):运行列表分页与跨进程恢复

### 已完成

- [x] `GET /api/runs` 重构:`?limit=`(1-100)/`?offset=`/`?status=` 过滤,按开始时间倒序,每条返回 `{run_id, status, reason_code, started_at, stopped_at, summary, simulated, agent}` 摘要;非法参数 400 `INVALID_QUERY`;不可读记录以 `UNREADABLE` 状态呈现,不静默消失。
- [x] 运行→Agent 归属索引持久化:`RunStore` 目录下 `.agent_index.json`(原子写、启动加载、schema 校验、损坏忽略),**跨进程恢复打通**——重启后的服务实例可对既有 BLOCKED 运行执行 resume(有测试)。
- [x] `RunStore.list_runs()` 跳过点文件,防止索引/临时文件被当作运行记录。
- [x] 前端日志视图:服务端摘要直出(消灭 N+1 请求),每页 10 条 + "加载更多",点击行懒加载完整检查器;前端资源 URL 版本化(`?v=`)对抗 webview 缓存。
- [x] 修复前端重复函数定义(旧版 `renderLogsView` 残留导致新版被覆盖),新增顶层函数重名自查。

### Latest Verification

`python -m unittest discover -s tests -q` 237 passed(170 基线 + Agent 外壳 19 + HTTP 服务 26 + 节点库 12 + 契约 10)。

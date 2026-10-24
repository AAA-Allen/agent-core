# Night Development Progress

主线：运行生命周期状态机

## Baseline

- 实际测试基线：142 passed（WorkflowRunner 终态输出契约固化后）
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

最近验证：`python -m unittest discover -s tests -q` 和禁用外部插件的 `pytest -q` 均为 142 passed。

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
- 当前改动尚未提交，等待人工审查后再决定提交边界。

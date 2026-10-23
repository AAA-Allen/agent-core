# Night Development Progress

主线：运行生命周期状态机

## Baseline

- 实际测试基线：115 passed（生命周期状态机接入后）
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

最近验证：`python -m unittest discover -s tests -q` 和禁用外部插件的 `pytest -q` 均为 125 passed。

## Known Limits

- 生命周期事件目前只在 Runtime 实例内存中保留，尚未接入持久化 Store。
- 同步 Python 节点不能被安全强制中断；取消和超时需要协作式边界。
- 取消请求在节点开始前或节点返回后确认；节点内部可通过 `ctx.cancel_requested` 协作检查。
- `timeout_seconds` 在节点边界确认，超时返回 `CANCELED`/`TIMEOUT`；无法强制中断同步节点。
- `RunStore` 保存终态 `RunResult` 及其生命周期事件；旧 JSON 记录缺少事件字段时按空事件兼容。
- `Runtime(store=...)` 会自动保存终态结果；新 Runtime 实例可从 RunStore 查询终态和生命周期事件。
- RunStore 另外原子保存 `<run_id>.events.json`，支持运行中的状态和事件查询；事件日志不计入 `list_runs()` 的运行结果列表。
- Store 写入失败会向调用方抛出异常，不会伪装成运行成功或静默丢失记录；内存生命周期仍保留真实终态。
- 仅支持外部线程发出协作式取消请求；不提供并行节点调度或通用线程安全承诺。

## Acceptance

- 生命周期状态机已接入 `Runtime.run()` 和 `Runtime.resume()`。
- 六种状态、合法转换、终态保护、查询、取消、超时和生命周期事件持久化均有测试。
- 未实现：强制中断、重试、并行调度、分布式锁和 exactly-once 外部副作用保证。

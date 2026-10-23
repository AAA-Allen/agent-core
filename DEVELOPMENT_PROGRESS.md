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
- [ ] E. 取消语义
- [ ] F. 超时语义
- [ ] G. 生命周期事件与持久化
- [ ] H. 并发边界与错误处理
- [ ] I. 全量回归、兼容性检查和文档更新

## Latest Verification

最近验证：`python -m unittest discover -s tests -q` 和禁用外部插件的 `pytest -q` 均为 115 passed。

## Known Limits

- 生命周期事件目前只在 Runtime 实例内存中保留，尚未接入持久化 Store。
- 同步 Python 节点不能被安全强制中断；取消和超时需要协作式边界。

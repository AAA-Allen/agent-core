# Night Development Progress

主线：运行生命周期状态机

## Baseline

- 实际测试基线：109 passed
- 执行模型：同步、单进程、节点顺序执行
- 现有 `RunStore`：保存完成后的 `RunResult`
- 现有 `CheckpointStore`：保存节点边界快照，显式 `Runtime.resume()`

## Subtasks

- [x] A. 现状检查和生命周期设计
- [x] B. 状态模型与合法转换
- [ ] C. Runtime 生命周期集成
- [ ] D. 运行状态查询
- [ ] E. 取消语义
- [ ] F. 超时语义
- [ ] G. 生命周期事件与持久化
- [ ] H. 并发边界与错误处理
- [ ] I. 全量回归、兼容性检查和文档更新

## Latest Verification

待运行：`python -m unittest discover -s tests -q`、禁用外部插件的 `pytest`。

## Known Limits

- 当前状态机只负责转换校验和事件内存记录，尚未接入 Runtime。
- 同步 Python 节点不能被安全强制中断；取消和超时需要协作式边界。

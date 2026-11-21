# 评审恢复队列轮转

## 问题与行为

旧扫描按pending优先及任务更新时间取前20条；活跃worker的长任务虽然因任务锁跳过，却始终留在扫描队首，后面的失联任务可能一直无法检查。

新增独立 nullable `EvaluateTask.recovery_checked_at`。短事务按 `coalesce(recovery_checked_at, created_at), id` 取批次，用 `FOR UPDATE SKIP LOCKED` 避免恢复器并发重复领取。领取时只更新检查时间、提交后释放行锁；即使任务仍被活跃worker持锁，也会排到未检查任务之后。去掉pending绝对优先，避免running一直被新pending插队。

检查时间与worker的updated_at/状态分离。工作是否过期仍按实际updated_at判断，重投仍要取得原任务会话锁、复核状态与投递窗口。扫描领取后崩溃只延迟该批次下一次检查，不删除任务。恢复领取事务不持有任务会话锁，避免与worker的会话锁→写行顺序构成反向等待。

## 文件与迁移

- `backend/app/services/evaluation_recovery.py`：新增claim_recovery_batch，沿用批次300秒、投递10秒和原ID消息策略。
- `backend/app/models/project.py`：独立检查时间与pending/running部分表达式索引。
- `backend/alembic/versions/a1001934_evaluation_recovery_rotation.py`：从a1001933升级，新增列与索引；不回填历史检查时间，旧任务用created_at排序。发布前执行迁移。普通索引创建需在发布窗口评估表规模和锁等待，未宣称在线无锁迁移。

## 证据

43项相关离线测试通过，涵盖扫描SQL、检查时间与worker状态分离、提交失败停止、既有恢复/worker锁/上下文/去重/期限逻辑。3项真实PG用例仅收集成功，覆盖持锁跳过后重投、队首活跃任务轮转、两个扫描器领取不同任务；数据库事务语义尚未实跑。

本地两轮shadow-mind复核（平台无可靠只读子代理隔离）：并发检查确认领取事务在任务锁之前结束、只写检查字段、提交失败不继续；完成度检查修正PG旧夹具created_at未过期的问题，并标明迁移和实际数据库未验证。

## 未完成事项

新增索引的真实EXPLAIN、海量历史任务/高并发吞吐及恢复延迟尚未测量。有限处理速率不能保证任意积压下固定时间恢复。真实线程发布超时、晚到消息及模型调用后落库前崩溃的重复费用风险仍在；本改动不声称恰好一次计费。

未迁移、未部署，未产生新中文小说正文或文学质量证据。整体计划继续。

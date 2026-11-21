# 整书生成的数据边界

## 章节选择和顺序

`pipeline_service.create_pipeline` 先验证项目存在且未删除，再通过 Volume 关联查询 Chapter，始终保留 project_id 条件。指定卷必须属于同一个项目。无卷或无章时直接返回校验错误，禁止退化成无条件查询。

`pipeline_chapter_query` 是任务执行和进度投影的共用顺序：卷序、卷内章序、稳定 ID。`chapter_idx` 是卷内编号，不能单独充当整书顺序。投影额外返回 chapter_id、volume_id、volume_idx，供调用方明确定位章节。

Worker 写入前再次核对章节所属项目与指定卷，保护旧版本已经创建的错误任务。这不是修复历史数据；越界任务被记录为 failed，正文保持不变。

## 正文和质量状态

- 模型返回为空，或清理后为空：在修改 Chapter 之前抛出错误；原正文、字数和状态都保持原样。
- 正文疑似截断或拒答：保留为 draft，子任务记为 needs_review，记录错误提示，不增加 completed_chapters，也不把此输出作为已完成正文派发实体抽取。
- 后续章节可以继续生成；本批结束后存在待审章节时，整书状态为 needs_review，并提供 needs_review_chapters 计数。
- 显式重新启动待审任务时，只将待审子任务恢复为 pending，已完成子任务不重置。
- 这些状态使用既有 varchar 列，本阶段不改变数据库结构。

## 验证边界

`backend/tests/test_pipeline_safety.py` 对真实 PostgreSQL 执行事务隔离测试，模型和外部实体派发用替身。只允许显式配置本地 `aiwrite_pipeline_test` 数据库；每个测试的 DDL 与数据在外层事务回滚。使用 `--noconftest` 避免导入整套应用的集成测试配置。

CI 的 pipeline-safety job 是阻塞测试。旧 backend job 中非阻塞 lint/mypy/pytest 基线尚需后续整治，不能把本阶段称为全套 CI 门禁完成。

## 尚未闭合的边界

暂停/崩溃恢复、重复启动互斥、提交后投递、统一单章主链、回滚与人工编辑冲突保护仍按 [P1 计划](../REPAIR_AND_QUALITY_PLAN.md) 推进。此阶段不应被解释为整书自动生产已经完成端到端验收。

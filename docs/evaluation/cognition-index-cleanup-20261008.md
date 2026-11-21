# 活跃作品认知索引治理与并发保护

2026-10-08。基线 `76f0075`。本批代码、离线回归与冻结本地复核完成，未部署，尚未清理真实服务的数据。整体修复、CPA 实稿对照及文学验收仍未完成。

上一批用当前快照过滤旧向量，解决了旧点参与召回的问题，但改稿、作者校订或换嵌入模型留下的点仍占存储。本批补充有来源核对的清理，并让同一作品的检索准备和清理共用索引锁；它只治理可重建的向量缓存，不修改正文、认知结果、作者决定或文学判断。

## 实现

- 新增 `cognition_index_lock`，使用独立于正文生成、实体物化和评审任务的 PG advisory lock 命名空间。锁连接独立且不持打开的事务；获取失败、取消和结束均丢弃物理连接，避免把会话锁带回连接池。
- 需要语义索引的检索在原有 180 秒总期限内等待该书索引锁，准备/检索结束及返回材料前检查连接。清理只尝试认领，遇到占用返回 `busy`。同书并发冷构建因此串行，后来的请求复用已经确认的片段；能完整放入提示的短历史仍不需要语义索引锁。
- `load_index_snapshot` 使用全部当前已完成章节的来源、完整认知载荷和作者决定构造清理快照，包括有效空决定。读取前后核对全书最大保存位置，删除前再次检查完整快照和当前逻辑编码器。缺失提取、坏来源、改稿、校订、重排或无法取得配置均不会被解释成“应该删除这些点”。
- 仅删除严格匹配本模块集合名称、完整版本 1 payload、作品、编码器、内容摘要和不可变 UUID 的过期点。当前点、其他作品、未知/未来 payload 和不匹配的集合配置保留；后两者在结果中计数。旧编码器集合只在确认为空后删除，删除回执须为完成状态。
- 每页请求 256 个点，每个活跃作品任务最多处理 64 页、120 秒；超出时返回集合/offset 游标。游标和集合清单均重新核对作品归属，下一批重新读取当前全书快照，不把游标当作来源证明。断连或取消后，可重新扫描已完成删除后的剩余点。
- 永久删除作品也使用同一索引锁，逐集合再次确认作品不存在；已删除集合的重复投递无害，回收站作品保留。整集合删除只报告集合数，不编造未测量的点数。
- 每小时的现有 `cognition.cleanup_deleted_projects` 入口只发现目录并派发，逐书执行 `cognition.cleanup_project_indexes`。总任务只读取一次全站集合目录、每 200 个作品批量核对状态，把每书集合清单传给子任务及续批；永久删除作品也独立执行。一个项目的慢清理或派发失败不会阻止后续项目的派发。
- 新代码没有 SQL schema 变更，复用现有来源回执及 `a1001949` 正文指纹；新增维护不调用嵌入模型或小说模型。若以后恢复旧来源或切回旧编码器，缺失派生点由现有检索准备流程重建。

## 复核与修复

首轮冻结复核发现：总任务先串行清理全站已删除作品，可能用完期限后尚未派发活跃作品；每书子任务还重复下载全站目录。两项针对性回归先失败，现改为纯发现/分书派发和携带集合清单，回归通过。

首轮全目录另发现一个已有 SQLite 上下文测试没有注入新增锁替身；离线隔离器拦截了其连接尝试。已补显式替身，锁生命周期由专门离线用例验证，真实 PG 用例保持服务跳过。没有通过放开网络或绕过测试入口解决。

第二轮冻结本地正确性/归属及架构/完成范围复核未发现新的阻断问题。依据 shadow-mind 的隔离要求，没有启动缺少可信只读隔离的子代理。12 个 Python 文件和 1 个计划文件在复核期间保持不变；随后仅更新文档。

代码及测试指纹：`bfa29b4a48991eb69ce3010ccbd271acae31ed7bfc015b946a401f1b4fdc44cb`。含当时 PLAN 的指纹：`1929982bcaccb1398841451f55b26d5f55e51d616c741f6ed2fcde2fed7e560b`。

## 离线验证

从 `/root/ai-write` 使用已授权入口：

```bash
/root/.aiwrite-evaluation/run-tests.sh --offline \
  tests/test_cognition_index_lock.py tests/test_cognition_index_cleanup.py \
  tests/test_cognition_index_cleanup_tasks.py tests/test_cognition_index_lock_pg.py \
  tests/test_cognition_index_maintenance.py tests/test_cognition_retrieval.py \
  tests/test_cognition_retrieval_sources.py tests/test_cognition_retrieval_entrypoints.py \
  tests/test_character_state_context.py tests/services/test_generation_runner.py -q -s

/root/.aiwrite-evaluation/run-tests.sh --offline tests -q \
  --deselect=tests/test_v10_observability.py::test_version_endpoint_public \
  --deselect=tests/test_v10_observability.py::test_metrics_endpoint_public
```

- 最终重点：**145 passed / 3 skipped**，28.90 秒。
- 最终全目录：**3072 passed / 664 skipped / 2 deselected / 2 warnings**，95.05 秒。两条警告来自既有短 JWT 测试密钥。跳过的服务用例、两项本机烟测排除均未计入真实服务通过。
- 新增 3 项 PG 会话锁测试覆盖互斥/事务边界、锁命名空间及取消等待者释放，均因离线模式未执行。离线用例另覆盖获取/提交失败、丢锁后不返回上下文、同书只嵌入一次、清理占用跳过、原文/作者决定/配置变化、未知载荷、游标继续、删除中断、异常回执和任务续派。
- 12 个 Python 文件 AST、`git diff --check`、源码迁移单头 `a1001949` 检查通过；本批没有新迁移或前端改动。累计迁移尚未在真实 PG 应用。

规模用例使用实际 SQLite、内存 Qdrant 和固定向量：1005 前章、1 目标章、5071235 字符、5021 条认知，另创建 5021 个旧编码器点。

| 清理结果 | 本次观测 |
| --- | ---: |
| 扫描点数 | 10042 |
| 删除旧点 | 5021 |
| 保留当前点 | 5021，全部保留 |
| 删除旧编码器空集合 | 1 |
| 嵌入调用 / 已验证正文回读 | 0 / 0 字符 |
| 来源 SQL 次数 | 82 |
| 离线清理耗时 | 1.95 秒 |

82 次 SQL **不含替身锁所代替的真实 PG 锁/检查语句**。元信息和认知载荷仍按全书读取，保留 ID 集合也随书增长；每个续批都重新核对，不是常量内存或整书固定成本。上述数字只反映本次合成规模用例，不能推断真实 PG、Qdrant、任务排队或语义质量。

本机日志：`/tmp/aiwrite-cognition-index-cleanup-20261008-focused-v4.log`、`/tmp/aiwrite-cognition-index-cleanup-20261008-full-v2.log`；复核文件同前缀 `freeze-v2.json`、`review-v2.md`。

## 发布边界与剩余验收

1. 真实 PG 会话锁、连接开销、同书排队、Qdrant 分页/删除与 Celery 投递尚未验收。同书语义索引操作现为串行，等待计入检索总期限；需要实际负载测量。
2. 发布时先让所有 API/worker 的索引调用方升级为共用锁，再启用新版周期清理；混合版本不具备这里的互斥保护。`rank_cognition`/`prepare_index`不能成为绕过锁的业务入口。
3. 未认识的 payload/集合配置保留；同一逻辑编码器下的不同维度集合、上游同名别名的实际模型替换不会被代码擅自判断为旧模型。配置和兼容性仍需核对。
4. 新建或迟到写入的点可能位于本轮游标之前，下一次周期扫描继续处理；占用或投递失败由后续周期再次发现。队列故障期间不能保证即时回收。源码中的周期入口不代表真实 Beat/Worker 已运行。
5. 没有调用 CPA、没有切换默认模型、没有生成新小说稿，也没有执行部署。本批不增加文学合格证据。Gemini 与其他候选仍应在固定上游、同一创作约束下生成连续章节，按[中文作者/编辑标准](CHINESE_FICTION_EDITORIAL_STANDARD.md)逐段审读；测试和向量分数不能替代这一步。

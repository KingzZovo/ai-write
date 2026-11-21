# 兑现核查执行与恢复（2026-10-08）

新增foreshadow_review_worker与Celery任务foreshadows.review_chapter / foreshadows.replay_reviews，Beat每60秒扫描。待处理任务按更新时间/ID排序，单轮最多20条；派发失败保留queued租期，10分钟后可再派发。每项最多3次执行，最后一次崩溃留下的超时running/queued会转failed，不永远悬挂。执行器以条件UPDATE认领并创建独立attempt_token，旧执行不能覆盖新令牌。

模型前校验当前已完成章节来源摘要，以及该章最大任务revision和仍待交接的来源通知revision，旧保存版本不调用模型。模型核查限时300秒；Celery软/硬上限360/420秒。核查后再次锁定章节来源、核对令牌和最新revision，在同一事务中保存建议并标记任务completed；收尾失败回滚已插入建议。失败或取消以异常类型记录failed，不把异常原文存入错误字段。

修正核查服务的成功语义：格式错误、字符串布尔、缺原句/解释、伪造原句、明确截断、来源变化现在抛ResolutionReviewIncomplete；合法布尔false仍是成功的否定核查。不得把模型出错当作“没有发现兑现”完成任务。存储增加strict_source用于执行器，来源变化不能以保存0条伪装成功；重复已有快照导致0新增仍可完成。

已完成章节的Batch post-hook不再同步重复模型核查，依赖事务来源通知及队列；保存草稿仍保留原有同步核查路径。消费者接通不改变“作者确认后才兑现”的约定。

验证：`/root/.aiwrite-evaluation/run-tests.sh tests/test_foreshadow_review_worker.py tests/test_foreshadow_resolution_suggestions.py tests/test_foreshadow_review_jobs.py tests/test_foreshadow_review_store.py tests/test_foreshadow_review_source.py tests/test_hook_chronology_failure.py`最终102 passed。Python编译及diff检查通过。SQLite事务验证任务完成与建议写入、最终写失败回滚、旧令牌不落库、来源/通知变更淘汰、取消失败状态、派发失败冷却、过期任务重派与重试上限。核查/来源解析用适配器，另有既有服务测试；任务名及Beat配置检查通过。

按shadow-mind在本地完成正确性与完成范围复核（无可信只读子代理隔离）。复核发现重试上限后的崩溃running需要终结，已补超时转failed及回归；同时检查令牌条件更新、事务收尾、批量重复调用和异常与无发现的区别，未发现本次阻断问题。

未完成：真实Celery/Redis投递、PG锁竞争/事务与触发器、进程强杀恢复和真实模型调用未验收。单轮核查当前ready/ripening集合，不证明完整书稿全部伏笔已被召回。任务状态展示、失败后作者重试、普通草稿主动核查入口尚未提供；高伏笔数量可能达到总超时。图谱来源确认/同步恢复仍未完成。迁移a1001936/a1001937未执行，代码未部署。

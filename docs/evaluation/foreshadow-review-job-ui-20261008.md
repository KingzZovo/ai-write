# 核查任务状态与作者重试（2026-10-08）

新增GET /api/projects/{project_id}/foreshadows/review-jobs，窗口函数按章节保留最新已入队revision，再分页返回状态、章节标题、尝试次数和更新时间；同时限制任务作品与章节所属作品，不公开source_json或异常详情。来源通知尚未交接时，最新已入队任务不一定对应最新保存版本，重试另行核验。

新增POST /review-jobs/{job_id}/retry。仅failed可重试，按执行器相同顺序锁当前章节来源和任务，再核对来源摘要、最新任务/通知revision及状态。通过后重置本轮attempts、清除旧attempt_token/error并设pending，提交成功才返回确认；无需直接调用broker，由现有恢复调度交付。跨作品404，过时/运行中/其他状态409，不能凭重复点击重置进行中的核查。

面板新增章节核查进度、失败重试、分页、刷新和加载状态。存在pending/queued/running时每5秒轮询当前页；任务完成或首次观察到已完成任务时刷新建议列表，全部终态后停止轮询。失败或响应不匹配不宣称重试已排队。无任务或没有活动任务时，可手动刷新发现后来入队的新任务。对审阅列表的测试隔离任务子组件，另用独立任务组件测试覆盖实际轮询与重试。

验证：`/root/.aiwrite-evaluation/run-tests.sh tests/test_foreshadow_review_worker.py tests/test_foreshadow_review_jobs.py tests/test_foreshadow_request_validation.py`，48 passed。SQLite实际读取/重试状态更新，覆盖最新revision筛选、隔离、不泄露内部数据、失效令牌、重试后执行、过期/跨作品/运行中拒绝及HTTPX路由提交。

前端 `vitest run src/__tests__/foreshadowReviewJobs.test.tsx src/__tests__/foreshadowReviews.test.tsx src/__tests__/foreshadowPlans.test.tsx`，最终20 passed；`next build --webpack`通过，类型检查、20页静态生成和构建追踪完成。Python编译/diff检查通过。

按shadow-mind在本地完成正确性与完成范围复核（无可信只读子代理隔离）。复核发现首次读到已完成任务时也需刷新证据列表，已修复并增加测试。核对来源前于任务的锁顺序、重复重试拒绝、响应确认和轮询停止逻辑，未发现本次阻断问题。

未完成：普通草稿主动核查入口、图谱来源确认/同步恢复、真实PG锁竞争/Celery投递/浏览器/认证联调。手动重试开启新一轮最多3次自动尝试，历史尝试总次数未另设审计字段。迁移a1001936/a1001937未执行，代码未部署；真实中文文学质量及模型对照仍未验收。

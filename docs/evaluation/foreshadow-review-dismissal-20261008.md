# 作者忽略兑现建议（2026-10-08）

新增POST /api/projects/{project_id}/foreshadows/reviews/{review_id}/dismiss。服务按作品+建议ID+pending状态执行条件UPDATE，只有未处理建议改为dismissed；重复忽略幂等成功，记录缺失/其他作品返回404，已接受等其他状态返回409。忽略不需要正文仍新鲜，因为它只处理建议，不更改伏笔事实或正文。

API显式等待本请求数据库提交后再返回ID和dismissed状态，不依赖yield清理阶段的提交时机。提交异常不返回成功。底层服务不自行提交，便于纳入其他事务。前端在确认响应ID/状态匹配前保留原句，处理中禁用忽略按钮；失败或无效确认保留建议并提示刷新核对，成功后移除并重新读取列表。

验证：`/root/.aiwrite-evaluation/run-tests.sh tests/test_foreshadow_review_store.py tests/test_foreshadow_request_validation.py`，44 passed。覆盖SQLite实际条件更新、作品隔离、幂等和accepted状态保护，HTTPX实际路由返回/提交调用，以及提交失败传播。前端 `vitest run src/__tests__/foreshadowReviews.test.tsx src/__tests__/foreshadowPlans.test.tsx`，11 passed，覆盖等待确认、错误/错ID响应保留原文。最终 `next build --webpack` 通过，类型检查、20页静态生成和构建追踪完成；Python编译与diff检查通过。

按shadow-mind在本地完成正确性及完成范围复核（无可信只读子代理隔离）。核对条件更新、状态不覆盖、API事务确认、前端异常和重新读取，无本次阻断问题。测试不证明真实PG竞争/网络断连提交时序、认证中间件或浏览器联调通过。

未完成：确认兑现的原子版本检查和跨存储处理、审阅历史/恢复入口、实际部署/迁移与真实小说验收。当前忽略记录仍可由status=dismissed列表API读取，界面仅展示pending。a1001936未执行，代码未部署。

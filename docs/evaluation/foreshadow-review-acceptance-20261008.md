# 作者确认PG原生伏笔兑现（2026-10-08）

新增POST /api/projects/{project_id}/foreshadows/reviews/{review_id}/accept，接入面板“核对后确认兑现”。目前只允许source=lifecycle且来源current的建议；图谱/未知来源的projection_current明确返回409，界面禁用确认按钮，仍允许忽略。跨存储确认尚未实现，本次没有把PG副本当作图谱权威状态。

服务先定位审阅记录，再按与建议存储一致的顺序锁章节/卷、伏笔和审阅记录。重读状态与两侧来源快照，正文/位置/版本/伏笔内容变化或证据无法核验时拒绝确认。只在同一事务内将伏笔标resolved、兑现章号设为该正文持久化global_idx、接近度设1，并把审阅状态标accepted。API提交成功才返回确认。

重复接受已accepted的建议只确认历史决定，不重放伏笔写入，因此不会覆盖作者后来撤销或修改的伏笔。已dismissed等状态不能接受，accepted也不能被忽略覆盖。所有查询按作品限制并禁用autoflush，事务内无模型/图谱网络调用。

前端仅在服务端返回相符ID/accepted后移除建议，并刷新父面板伏笔状态；失败保留原证据。来源changed/unknown/projection_current禁用确认，不以读取时的标签替代后端再次校验。

验证：`/root/.aiwrite-evaluation/run-tests.sh tests/test_foreshadow_review_store.py tests/test_foreshadow_request_validation.py`最终53 passed。覆盖SQLite双记录事务更新、重复接受不重放、第二次写失败整笔回滚、来源变更/跨作品/已忽略/图谱来源拒绝、HTTPX实际路由状态与提交确认、提交失败不确认。PG锁顺序只做SQL编译检查，非真实并发验证。

前端 `vitest run src/__tests__/foreshadowReviews.test.tsx src/__tests__/foreshadowPlans.test.tsx`最终16 passed；`next build --webpack`生产构建通过，类型检查、20页静态生成和构建追踪完成。Python编译/diff检查通过。

按shadow-mind在本地完成正确性和完成范围复核（无可信只读子代理隔离）。核对锁顺序与存储链、重复提交语义、调用者提交时机、错误保留证据以及PG限定范围，无本次阻断问题。

未完成：图谱来源兑现需实现同步/恢复策略；因此完整确认工作流尚未覆盖所有来源。实际PG竞争、删除/移动并发、跨存储恢复和浏览器/认证联调尚未验收。审阅历史现在保留的是确认前快照，accepted后的来源差异不能直接解释为作者改稿，历史展示仍待完善。a1001936未执行，代码未部署；真实小说质量与模型对照继续待验收。

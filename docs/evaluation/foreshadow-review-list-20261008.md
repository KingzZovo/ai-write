# 兑现建议审阅读取（2026-10-08）

新增GET /api/projects/{project_id}/foreshadows/reviews，默认pending，可筛选accepted/dismissed；limit默认20、最大100，offset非负，按created_at/id倒序，返回has_more。读取按作品隔离，分页后批量加载相关章节和伏笔，非空页固定三次查询；不会调用模型或修改审阅状态。

新reader核对存储快照摘要、当前正文/位置/版本/状态、伏笔内容摘要及引文范围。source_state为current（PG lifecycle当前一致）、changed、unknown、projection_current（图谱或未知来源仅PG记录一致）。unknown时不公开未经核验的来源元数据，以免错误嵌套数据影响渲染。读取一致只是观察结果，不能授权后续直接确认。

ForeshadowPanel已嵌入独立审阅列表，展示原句、核查说明、快照章号和来源标签，明确建议尚未确认。支持20条分页、失败重试和手动刷新；本地伏笔编辑/确认/删除触发列表刷新，切换作品时重新创建组件并忽略旧请求返回。空列表保留刷新入口。current文案为“读取时与稿件及伏笔记录一致”，避免承诺未来时刻仍有效。

验证：后端 `/root/.aiwrite-evaluation/run-tests.sh tests/test_foreshadow_review_store.py tests/test_foreshadow_request_validation.py tests/test_foreshadow_review_source.py` 最终53 passed。覆盖SQLite真实数据读取、来源变化/快照损坏/越界、作品隔离、图谱副本不当作权威来源、HTTPX挂载路由响应及分页参数校验。

前端 `vitest run src/__tests__/foreshadowReviews.test.tsx src/__tests__/foreshadowPlans.test.tsx` 最终8 passed。覆盖引文/未确认标签、失败重试、分页、旧作品迟到响应、本地修改后的重新核验，以及原有伏笔卡片显示。最终 `next build --webpack` 通过，类型检查、20页静态生成及构建追踪完成；Python编译/diff检查通过。中间一次修改脚本目录写错没有写入，随后在正确路径修改，以上结果均为修正后的最终验证。

按shadow-mind在本地完成正确性和完成范围复核（无可信只读子代理隔离）。发现本地修改后来源标记未刷新，已补联动与回归；另检查损坏快照可能带不合法嵌套数据，已过滤unknown元数据。最终未发现本次阻断问题。

未完成：确认/驳回操作及确认时锁定重验，图谱来源跨存储一致性，实际浏览器/认证/PG并发联调。自动钩子目前仅批量生成链已核对，其他入口覆盖尚待验证。迁移a1001936仍未执行，代码未部署；部署必须先应用迁移。无法据本轮测试宣称真实中文小说质量或完整审阅工作流已达标。

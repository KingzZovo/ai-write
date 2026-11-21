# 整书大纲确认绑定与旧稿保护

2026-10-07。本批代码未部署；隔离运行仍为 c4b4a4b，生产未更新。

## 问题与改动

桌面整书 SSE 中断后仍可点击确认；未取得 saved ID 时按 UUID 寻找另一份未确认大纲。刷新页面则把任何选中的书纲标成已确认，直接进入第二步。内联编辑只提交 raw_text，覆盖分卷计划等结构字段。

- 新增 `frontend/src/lib/bookOutline.ts`：仅在 saved + completed 且无业务/传输错误时读取该 saved ID，验证项目/层级/非空正式文本；EOF、阶段 done、单独 saved 均不算成功。读回前不开放编辑/确认。
- 桌面确认只提交当前展示的已保存记录及 expected_content_json，移除查找替代记录；刷新按确认状态、创建时间、版本选择，未确认保持第一步。生成错误与正文分开；旧请求在换项目/重新生成后不更新书纲。
- 内联保存保留 content_json 其余字段并提交原内容快照，改稿后解除确认；编辑未保存时不能确认，取消编辑恢复已保存文本，保存期间禁用文本框。分卷计划保存使用绑定书纲 ID，修复确认后清空临时 ID 导致不保存的问题；刷新恢复计划。
- `PUT outlines/{id}` 与 `POST outlines/{id}/confirm` 在锁定、刷新行后校验可选内容快照，不一致返回409。旧客户端不传快照仍兼容。响应补 created_at，避免 UUID 被当时间。

## 验证

- 前端全量：15文件 / **136 passed**，包括新增14项书纲流结束/错误/身份校验/选择测试；现有卷向导测试仍通过。日志 `/tmp/aiwrite-book-frontend.log`。
- 后端离线：`tests/test_outline_confirmation_unit.py` **10 passed**，覆盖确认/编辑的成功、变化、丢失、错误项目、旧请求兼容以及锁查询。日志 `/tmp/aiwrite-book-backend.log`。
- TypeScript、Python AST、git diff --check 通过。
- 新增 `tests/test_outline_confirmation_pg.py` 3项真实PG测试：旧会话缓存不能确认/覆盖并发修改，保存后确认保留计划。当前网络权限禁止PG连接，**未执行**，不能以模拟session证明真实行锁行为。
- `npm run build` 失败：Google Fonts Geist/Geist Mono 无法联网下载；不修改依赖、不伪造字体响应来宣称构建成功。日志 `/tmp/aiwrite-book-build.log`。
- 没有可信的只读子代理隔离，按 shadow-mind 使用本地两轮独立复核。正确性复核发现保存期间继续输入可能被返回稿覆盖，已禁用保存时编辑；流程复核发现刷新未恢复计划，已补齐。修改后重跑TypeScript。

## 验收缺口及后续

本批未做真实桌面浏览器/API联调、完整PG回归或隔离发布。上一批细纲源保护的数据库复验仍待完成。本批不修整书后端 detached save 的源快照问题，也不代表所有编辑入口已实现并发保护：OutlineEditor、卷规划PATCH及卷纲级联仍需独立审查。内联文字编辑保留structured字段，结构与修订文字的语义同步仍待专门处理。不得将本批视为整体完成。

网络恢复后的关键测试：

```bash
/root/.aiwrite-evaluation/run-tests.sh -q tests/test_outline_confirmation_pg.py tests/test_outline_confirmation_unit.py
/root/.aiwrite-evaluation/run-tests.sh -q tests/test_chapter_outline_save.py tests/test_outline_expansion_registry.py tests/test_chapter_planning_scope.py tests/test_context_cross_volume.py tests/test_outline_readiness.py tests/test_volume_creation.py --tb=short
```

本机CPA配置再次只读核对：8317端口，三个OpenAI兼容配置组含gemini-3.1-pro等别名。没有修改共享配置，也没有在网络受限条件下重新调用。已有同消息实写对比仍以 `scene-literary-20261007.md` 为准，不能由模型名称推断中文文学质量或底层模型身份。

# 卷纲编辑时保留独立细纲

2026-10-07。本批未部署，真实数据库验收未完成；隔离运行仍为c4b4a4b。

## 修复的问题

旧PUT大纲接口每次保存卷纲都会遍历chapter_summaries，按列表位置无条件覆盖Chapter.outline_json。仅修改raw_text也会把已扩写细纲替换成粗摘要；改变摘要顺序会写错章节。级联异常被吞掉，不能可靠区分完整同步与失败。

新增 `backend/app/services/volume_outline_sync.py`：

- raw_text等修改若未改变chapter_summaries，不操作章节。
- 明确章号按chapter_idx匹配；全部无章号的旧数据可按旧位置兼容，混用、重复、非法章号拒绝。所属volume_id/volume_idx不可借卷纲编辑重定向。
- 只有草稿状态、无正文、当前细纲与旧摘要完全相同的章节才自动同步。独立改稿、扩写细纲、已有正文、生成中章节、无旧摘要依据及被移除的章号全部保留。新增章号不自动创建，删除摘要不删除章节。
- 只改outline_json，不改标题、正文、事实摘要、状态或字数预算。全部验证完成后才应用修改。
- API按项目→卷纲→分卷→章节顺序加锁并刷新数据，卷纲和同步的摘要由同一事务提交。删除吞错的best-effort级联，失败传回调用方，由事务依赖回滚。
- 响应增加synced_chapters/preserved_chapters/unmaterialized_chapters；OutlineEditor显示已同步、需核对细纲、尚无对应章节数量。旧响应字段兼容。

## 验证

- 48项后端离线：14项新增同步规则测试，加书纲保存与确认回归。`/tmp/aiwrite-volume-sync-unit.log`，1.37秒。
- 前端全量16文件137项通过，包含编辑器保存后显示保留/缺章提示的组件测试；TypeScript通过。日志 `/tmp/aiwrite-volume-sync-frontend.log`、`/tmp/aiwrite-volume-sync-tsc.log`。
- AST与diff检查通过。
- 新增 `test_volume_outline_sync_pg.py` 4项真实PG用例，覆盖正常保留与同步、仅改文字、错误章号、提交失败整体回滚。**未执行**；环境网络限制未解除，不能把离线对象测试当作事务证据。
- 现有 `test_summary_routes.py` 事实摘要回归调整：已有正文时章节细纲同样应保留，而不是期待被新计划覆盖；事实摘要及来源标记仍须保持。该PG测试本轮也未执行。
- shadow-mind采用本地独立复核（无可信只读子代理隔离）。数据安全视角核对锁顺序、身份校验、写入前完整校验和保护条件；流程视角检查保存提示和新增/删除章号不隐式创建/删除章节。显式分卷ID指向不存在/其他作品时已补拒绝，不能当作普通未建卷静默处理。

## 待完成

真实PG回归、浏览器联调、隔离部署和全量后端验证待完成。前端生产构建上轮因Google Fonts网络下载失败，本轮未重复同一已知失败。OutlineEditor的重复/迟到保存、旧快照覆盖和raw_text与structured语义同步仍需后续修复；本批只修服务端隐式级联与结果提示，不据此宣称完整工作流或生成质量合格。

```bash
/root/.aiwrite-evaluation/run-tests.sh -q tests/test_volume_outline_sync_pg.py tests/test_summary_routes.py tests/test_outline_confirmation_pg.py tests/test_book_outline_save_pg.py
```

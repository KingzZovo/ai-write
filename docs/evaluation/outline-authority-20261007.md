# 作者文字修订与旧结构分离

2026-10-07。代码未部署，真实数据库和模型实写尚未验收。

## 问题和约定

此前细纲正文提示词主要读取summary/key_events等字段，可能忽略作者在raw_text中写的新稿；书纲提要也优先取旧premise/main_plot。保留旧结构用于查看，不能等同于允许它继续覆盖作者修订。

新增 `outline_authority.py`：

- 官方书/卷纲PUT、章节PUT在文字改变且存在结构字段时记录服务端生成的 `_author_text_revision` 内容摘要。纯文本或纯卷章数无需额外标记；已有标记不能通过普通保存随意移除，删除raw_text后以完整结构编辑才退出文字优先状态。
- `outline_for_generation`只生成供下游使用的数据视图，不修改存储。对有修订标记的稿件保留raw_text、身份/篇幅字段及分卷计划中的idx/est_chapters，省略旧情节、人物状态和其他叙事字段。原结构仍保存在原记录里。
- 接入书→卷、书/卷→章的普通与阶段生成、分卷章摘要提取、当前章/卷上下文、场景规划细纲、实际章节扩写及前章规划。正文仍独立使用事实记忆，本批不把计划改成已发生事实。
- 标记后的空文字不能借旧结构通过就绪检查。旧的退化/失败标记仍保留其拒绝规则。
- 经过校验的新结构化扩写清除旧raw_text和文字修订标记；旧state_changes也不能通过合并逻辑重回新稿。未修改的结构化生成结果保留原消费方式。

## 证据

- **117 passed, 3 deselected**（1.11秒）。覆盖新增10项权威文本测试及确认、细纲CAS、卷纲同步、扩写源快照、篇幅、作者结果约束、场景编排回归。日志 `/tmp/aiwrite-outline-authority.log`。
- 具体反例：旧结构“母亲已经去世/办葬礼”，作者新稿“母亲还活着…打电话”。当前章渲染、卷纲渲染、场景规划、书纲提要、实际chapter generator消息、扩写提示词均排除旧事件并保留新文本。结构化扩写不恢复“去世”的人物状态。
- 卷章数预算保留、旧结构存储未修改、无修订的结构化数据不变、清空文字不回退旧稿均有检查。
- Python AST、git diff --check通过。本批无前端代码修改，未重复前端全量和已知因字体网络下载失败的构建。
- `test_chapter_project_scope.py`新增1项真实ASGI/PG用例，验证官方保存标记→数据库读回→正文提示词使用新稿；旧卷纲编辑PG用例补标记预期。这些PG用例**未执行**，3项实际扩写/歧义源用例明确排除。
- shadow-mind本地独立复核（无可信只读子代理隔离）：来源/状态视角发现扩写的旧state_changes合并会漏回，已修正；生成契约视角确认未修改结构不被无条件删掉、正文预算与卷章定位仍保留，并补实际生成消息测试。

## 不能据此宣称完成的范围

这是后续显式编辑的来源治理，不是模型理解并自动重建结构。历史上已有但未标记的raw_text/结构冲突仍需识别核对。已提取的人物卡、世界规则、伏笔状态、其他未经过该视图的管理/导出路径，以及下游既有章节是否符合上游新剧情，仍需单独审计。未通过编辑接口写入数据的脚本也不会自动获得标记。

真实PG、完整后端、隔离部署、浏览器联调和中文小说实写复测待网络恢复。不能用上述构造反例或测试数证明文学质量合格。

```bash
/root/.aiwrite-evaluation/run-tests.sh -q tests/test_outline_authority.py tests/test_outline_confirmation_unit.py tests/test_chapter_outline_edit_unit.py tests/test_volume_outline_sync_unit.py tests/test_outline_expansion_snapshot_unit.py tests/test_chapter_planning_scope.py tests/test_author_outcome_prompts.py tests/test_c1_scene_orchestrator.py -k 'not actual_expansion and not sse_rejects_ambiguous'
# 网络恢复后
/root/.aiwrite-evaluation/run-tests.sh -q tests/test_chapter_project_scope.py tests/test_volume_outline_sync_pg.py tests/test_outline_expansion_registry.py tests/test_chapter_outline_save.py
```

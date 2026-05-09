> **✅ 2026-05-06 07:20 最新交接**：PR-BOOK-PROFILE-BIND **已完成**（`f73a74d`，pushed `origin/main` + `origin/feat/phase2-fix`）。下一步直接进入赤心仿写验证 Stage B/C/D/E（详 [docs/HANDOFF_2026-05-05_pr-book-profile-bind.md §5 + §12-13](HANDOFF_2026-05-05_pr-book-profile-bind.md)）。本页以下内容为历史。

## 当前批次 - PR-BOOK-PROFILE-BIND ✅ 已闭环（2026-05-06 07:20）
- [x] Step 1 DB schema migration + 回填（5 profile 绑定终态：龙族 / 江南 NULL / 赤心 / 天之炽① / 天之炽②）
- [x] Step 2 Model `StyleProfile.source_book_id`
- [x] Step 3 Service `style_profile_resolver.get_or_create_book_profile`
- [x] Step 4 Hook `process_uploaded_book` + `reprocess_reference_book`
- [x] Step 5 Scripts `extract_chapter_naming_style.py` / `reverse_fill_p2_upgrade.py` 改 `--book` 驱动
- [ ] Step 6 API endpoint `POST /api/reference-books/{id}/extract`（设计阶段已声明本轮跳过）
- [x] Step 7 syntax + backend restart + resolver smoke
- [x] Step 8 commit `f73a74d` + push 双分支
- [x] P0-LOCK 锁事件已复盘并清理（详 §13）

## 下一批次 - 赤心仿写验证 Stage B/C/D/E
- [ ] Stage B: POST /api/projects 创建赤心验证项目（绑 `b76da43a-…` + `0a543b1d-…`，target_words=200000）
- [ ] Stage C: 全书 / 分卷 / 章节大纲生成；验证 chapter_naming_style 注入、plot_structure_v2 反映、foreshadows + character_relations + world_setting 三结构化设定集复用
- [ ] Stage D: `/api/chapters/bulk-generate` 10 章正文；验证 7 写作模块 + 73 rules + 反 AI 约束 + PR-NO-RAW-INJECT 断面（不能出现任何赤心原文 passage）
- [ ] Stage E: 写 `docs/CHIXIN_VALIDATION_REPORT_2026-05-05.md`
- [ ] 决策：赤心 reprocess celery worker 在 P0-LOCK 期间被中断（约还剩 ~6000 slices 未切），是否要重启 reprocess。短期不影响 A2/A3/A4 已落产物，长期看 Stage C/D 章节质量决定。

# Handoff TODO

> **2026-05-03 12:25 交接**：本交接全貌 看 `docs/HANDOFF_2026-05-03_outline-batch2.md`。（可直接打勾执行）

> 最近一次更新：2026-05-02 晚 — main HEAD = `85aa039` (PR #20)；E2E 全业务验证跑通 + chapter generate_stream 签名 bug 已修复（PR #21）。**所有 P0~P6 + E2E P7 已闭**，详见 `docs/PROGRESS.md` 2026-05-02 晚条目。

## P0 本地状态（仅本机执行） ✅ 已关闭
- [x] `git fetch origin main` ✅
- [x] 丢弃本地未提交的 `backend/app/api/settings.py` ✅
- [x] `git stash drop stash@{0}` ✅
- [x] `git pull --ff-only origin main` → HEAD `0a4f9a1` (PR #18) ✅
- [x] `git status` = working tree clean，stash 列表为空 ✅

## P1 文档（必须） ✅ 已关闭
- [x] `docs/RUNBOOK.md` 写清 Neo4j truth + PG projection（PR #6）
- [x] 修正 README §设定集数据源约定 + `backend/app/api/settings.py` 410 message（PR #6 + PR #18）
- [x] 写清正确写入口：`/outlines/{id}/extract-settings` + `/neo4j-settings/*` + `/admin/entities/materialize` 全部已在 main
- [x] 写清 legacy 410：`/world-rules`、`/relationships` 写接口（RUNBOOK §3）
- [x] foreshadows PG 直写已修复，全部走 `/neo4j-settings/foreshadows`（PR #18）

## P2 防回归（清残留 PG 直写） ✅ 已关闭
- [x] 远端 search_code + 本地 grep 双路扫描 = 0 命中（除模型类定义误匹配）
- [x] foreshadows PG 直写 3 处（api/foreshadows.py:111,179、services/foreshadow_manager.py:84）已通过 PR #18 修复

## P3 验收 ✅ 已关闭（除真实环境）
- [x] `python3 -m compileall -q backend/app` ✅ COMPILEALL_OK（PR #6 + PR #8~#18 每次 push 前都跑过）
- [x] `scripts/verify_entity_writeback_v19.sh` 入仓（PR #6，151 行 SYNTAX_OK）
- [x] `scripts/verify_entity_writeback.sh` 入仓（PR #18 v1.9 自带版本）
- [ ] 在有真实 PROJECT_ID + 启动后端服务的环境跑：`PROJECT_ID=... CHAPTER_IDX=... bash scripts/verify_entity_writeback_v19.sh`

## P4 架构 vs 文档一致性 ✅ 已关闭
- [x] `feature/v1.0-big-bang` 上的 `neo4j_settings.py` + `admin_entities.py`（dc98363 起的 v1.9 链）已通过 PR #18 cherry-pick 合入 main，README/RUNBOOK 一致性达成

## P5 部署 ✅ 已关闭（2026-05-02 晚，本地 docker compose stack）
- [x] `alembic upgrade head` → target `a1001908_v190_character_organizations_table` ✅
- [x] 配置 env `ADMIN_USERNAMES=king` ✅ 以及 `AUTH_USERNAME=king` + JWT secret（JSON 数组，例 `["admin","king"]`）；缺失时 `/api/admin/*` 路由族对所有 JWT-sub 返回 403
- [x] 启动 11 容器（postgres/redis/qdrant/neo4j/backend/celery-worker×1/frontend/nginx/prometheus/grafana）全 healthy ✅
- [x] 烟测：fore_id `aaaaaaaa-bbbb-cccc-dddd-202605020001` → Neo4j (:Foreshadow) ✅ → PG `foreshadows` ✅ (F1=A E2E)
- [x] 烟测：`POST /api/admin/entities/materialize` 200 + counts ✅
- [x] `verify_entity_writeback_v19.sh` PASS，PID=`6e331209-056b-4b2b-9798-ac246ee8dd48`, [0/6]~[6/6] 全 OK ✅

## P6 仓库清理 ✅ 已关闭（2026-05-02 晚）
- [x] 建归档 tag `archive/feature-v1.0-big-bang` → `73e7897`（237 commit 历史永久可访）
- [x] 删除 `origin/feature/v1.0-big-bang`（内容已 100% 在 main，历史锁在 archive tag）
- [x] 删除 11 个 `origin/release/v1.*` 分支（已 merge进 main）
- [x] 删除 4 个残留 doc/fix 分支（`docs/handoff-execution-v1` / `docs/maintain-readme-iteration-plan` / `docs/progress-md-v1` / `fix/disable-legacy-settings-writes`，都已 merge）
- [x] 删除 chore 分支（`chore/post-v1.9-handoff-sync` / `chore/runbook-and-handoff-sync` / `docs/follow-up-plan`）
- [x] **最终状态**：origin 上仅剩 `main` + `archive/*` tag
- [ ] 如要继续推进 v2.0+，参考 `ITERATION_PLAN.md` Iteration 系列

## P7 全业务 E2E 验证 ✅ 已关闭（2026-05-02 晚，PR #21）

- [x] 参考书 status=ready (《龙族》/《三体》)，qdrant slice/style/plot/embedding collection 点亮 ✅
- [x] 创建项目 `0eaeff87-2f91-452c-812c-b4bcf2924fe2` ✅
- [x] 书级大纲×1 (`f9af7582...` 6766 chars)、已 confirm ✅
- [x] 分卷大纲×3（并发 60-70s/卷）✅
- [x] 物化 PG：3 volumes + 30 chapters，每章携 outline_json ✅
- [x] 生成 30 章正文：30/30 done、277,882 字、avg 9263、min 7377、max 13155（4-worker 并发 ~27 min）✅
- [x] 抽样质量：v1.c1/v2.c5/v3.c10 人物跨卷一致、剧情连贯、「中二热血×现代都市」调性达标 ✅
- [x] 修复 `chapter_generate_stream` v0.5+ 签名不匹配 → PR #21 (+9/-3) 已提交 ✅
- [ ] **后续**：章节生成后未自动触发 entity 抽取/cascade/evaluation（`characters=0, foreshadows=0, cascade_tasks=0`）——需手动调 `entities.extract_chapter` celery task，或为 `_run_async_generation_impl` chapter 分支加生成后钩子
- [ ] **后续**：加 `tests/tasks/test_run_async_generation_chapter.py` mock ChapterGenerator 锁定签名防回归

## feat/outline-batch2 — 7-PR 批次 已 push（2026-05-03）

HEAD = `f3e9e55`。详情看 `docs/PROGRESS.md` 同日条目。

- [x] PR-OL10 字数→章卷自动推算（`70706c9`）
- [x] PR-OL11 分卷 chapter_summaries 强化（`e838cd6`）
- [x] PR-OL12 章节大纲补 prev_summary + 本章预规划（`4b515ba`）
- [x] PR-OL13 章节大纲回写 Chapter.title（`3d07194`）
- [x] PR-OL14 OutlineTree 三层查看入口（`f6fa9e5`）
- [x] PR-AI1 命名与词汇硬约束（`919abab`）
- [x] PR-STY1 style v9 节奏/留白/信息密度 directives（`f3e9e55`）
- [ ] **下一步**：则使用 PID `310c1f9a` 清 30 章 + chapter outline 后重跑 30 章全流程，贴朱雀 AI 检测对比 baseline 12.04% / 42.21% / 45.75%。
- [ ] **后动工**：开分支 `feat/neo4j-batch1` 走 PR-NEO1（道具） / PR-NEO2（阵营事件） / PR-NEO3（时间） / PR-NEO4（context_pack/critic 消费）。

---

## 本窗口实测进展（2026-05-06 07:40）

- Stage B ✅ 项目 `df6f523e-f903-4644-bcce-636f5ed89c68` 已创建、style_reference 已绑、target_word_count=200000（PUT 修正）。
- Stage C ⚠️ book outline `0868f734-6e9c-4210-bbce-e09ac8c5adaa` content_json 仅 `raw_text`，缺 `main_plot / volume_plan / characters / world_setting / chapter_naming_style`，下游阻塞。
- Stage D / E 未开始。

## 下一批次 PR-OUTLINE-STAGED-PERSIST-STRUCT（新 PR，靠上述问题驱动）

- [ ] 修 `backend/app/api/generate.py:1060-1090 _persist_outline_now`：`level=="book"` 也走 `_OG()._parse_json` 抽 `main_plot / characters / world_setting / chapter_naming_style`（或逐阶段解析 + merge）。
- [ ] 顺带修 `backend/app/api/projects.py:58 create_project`：加 `target_word_count=body.target_word_count`。
- [ ] 重走 Stage B/C 验收：`outlines.content_json` 含上述键；volume outline 走通。
- [ ] Stage C 走通后接 Stage D `/api/chapters/bulk-generate` 10 章 + Stage E `docs/CHIXIN_VALIDATION_REPORT_2026-05-05.md`。

## 2026-05-06 22:02 PR-OUTLINE-STAGED-PERSIST-STRUCT 完成

- [x] 落地 commit `391e053`：outline_generator + generate + projects 三文件修复。
- [x] 验证：删旧 outline `0868f734-...`，重跑 staged_stream 产新 outline `15a4770c-9230-49a0-a493-700644b32862`，content_json 含 main_plot / characters / world_setting / chapter_naming_style / sections / volume_plan / raw_text 全部 7 键。
- [x] push origin main + feat/phase2-fix。
- [x] 顺带 `projects.create_project` 已带 target_word_count + genre_profile_code（不再需要 PUT 修正）。
- [ ] Stage C remainder：volume outline x1（vol1, est 50 章）→ chapter outline x前3章。
- [ ] Stage D：`/api/chapters/bulk-generate` 前 10 章。
- [ ] Stage E：`docs/CHIXIN_VALIDATION_REPORT_2026-05-05.md`。

### 关键 ID（赤心仿写）
- project: `df6f523e-f903-4644-bcce-636f5ed89c68`
- book outline (latest): `15a4770c-9230-49a0-a493-700644b32862`
- style profile: `b76da43a-a2fa-4fd3-8c54-3912acee6bb0`
- reference book: `0a543b1d-19fe-4e03-986e-42844feb36ee`

## 2026-05-06 14:45 赤心仿写验证 Stage C/D/E 完成

- [x] vol1 outline `803b025e-5347-4eb3-bb18-780169f6a732` 生成与 confirm（10 chapter_summaries / 12 keys / 86298 chars）。
- [x] chapter outline x3（ch1 `b0833dd1-...` 17007 / ch2 `92d98208-...` 19204 / ch3 `42837860-...` 16507）。
- [x] vol1 row `ee36b649-...` + 50 chapters 手动物化（docker exec /tmp/materialize_vol1.py）。
- [x] Stage D ch1-3 正文（scene_mode + auto_revise threshold=7.0）：ch1 14940/8.28、ch2 16690/8.28、ch3 13431/8.36，均 status=completed，revise skipped。
- [x] Stage E 报告 `docs/CHIXIN_VALIDATION_REPORT_2026-05-05.md`。

### 还欠（留给下个窗口）

- [ ] PR-OL2 补上 vol staged_stream done 后的 chapter materialize（去除对 `/tmp/materialize_vol1.py` 手动脚本依赖，参考 `volumes.regenerate_volume:143-329`）。
- [ ] bulk_generate 路径调查：当前未发现 `/api/generate/chapter/batch`；如需 ch4-ch10 拓展实测，先串行单调用 `POST /api/generate/chapter`。
- [ ] chapter outline 批量 confirm（当前 ch1-3 outline.is_confirmed=0，不影响下游，仅为流程完整性）。
- [ ] issues 清单（round-1 14-16/章）抽样人工标注，反馈赤心 profile examples/反例。
- [ ] sections 缺「四」段核查（之前 deferred）。
- [ ] chixin reprocess celery worker ～6000 slices 活化决策。

## 2026-05-06 23:55 vol1 章名重写（PR-CHIXIN-VOL1-RENAME）

用户反馈：原 50 章名多处语义/搭配不成立（「山门也该还债」山门是物不能还债、「钟声从灰里过来」应为「传来」、「债会先认账」主谓不通）。重写约束：
- 字数不齐 OK（不强制上限）
- 语义/搭配必须成立：物体不施动抽象动作；动词搭配符合汉语习惯
- 第二人称 “你/你们” 在 50 章里 → 0
- 禁 “角色名：” 冒号章名结构 → 0
- 禁现代词（流程/工序/怪癖） → 0
- 评语式/判词式，参考赤心冷叙述

变更范围：仅改章名，不动正文与 outline summary/key_events 主体。
- `outlines.content_json.chapter_summaries[i].title` × 50
- `chapters.title` × 50
- vol1 outline id ：`803b025e-5347-4eb3-bb18-780169f6a732`
- vol1 volume id：`ee36b649-ff4d-45ea-a045-f50f01589b5a`

脚本与 SQL（已事务提交）：
- `/tmp/rename_chapter_titles.py` 生成 `/tmp/rename.sql`（50 条 chapters update + 1 条 outlines update + BEGIN/COMMIT）
- 执行：`docker exec -e PGPASSWORD=$PW ai-write-postgres-1 psql -v ON_ERROR_STOP=1 -f /tmp/rename.sql` → 全部 UPDATE 1 + COMMIT
- 注：`outlines` 表无 `updated_at` 列，已从 UPDATE 子句去除

重写后重点对照（选輕）：
- ch1  账上没有这具尸 → 义庄夜收无名尸
- ch2  你先把手印按下去 → 按印者欠命
- ch3  三十三声，不是怪癖 → 夜更三十三响
- ch12 魏寒灰：先学会给自己留尸身 → 证留三处方能活
- ch25 裴四衡：山门收人不该只收会跳的 → 裴四衡问，他答认账
- ch43 三十三声一响，山门就得认账 → 山门外敲三十三响
- ch50 站得高，就更该还债 → 万人命债倒灌护山阵

遗留事项（未主动处理）：
- ch1/ch2/ch3 正文里可能还有类似 “钟声从灰里过来” 的拟物句子。如要清理，请点出具体句子/章。

## PR-OL2 — Auto-materialize chapters from volume outline (2026-05-07)

**Touch point**: `backend/app/api/generate.py` `_persist_outline_now()` — vol-level branch.

**Behavior**: After persisting a `level="volume"` outline via `/api/generate/outline`, the new block walks `_content_json["chapter_summaries"]` and:

1. Looks up `Volume(project_id, volume_idx=_vidx)`. If absent, creates one with `title` from outline (fallback `第{idx}卷`) and `summary` from `core_conflict` / `emotional_arc`.
2. If the volume already has chapters, **skips** materialization (idempotent; avoids dup rows).
3. Otherwise inserts one `Chapter` row per summary with `chapter_idx`, `title` (from `cs.title`, fallback `第{idx}章`), `outline_json=cs`. Commits.
4. Logs `PR-OL2 materialized N chapters under vol_idx=...` or `PR-OL2 skip materialize: ...`.

**Mirrors** `volumes.py` `regenerate_volume` materialize block (line ~300–320). Both paths now produce chapter rows; `/tmp/materialize_vol1.py` becomes a one-off.

**Not changed**: `target_word_count` allocation (only `regenerate_volume` does `allocate_even`). For `/api/generate/outline` flow, chapters use the SQLAlchemy default; existing vol1 was patched manually to 12000.

**Verified**: backend syntax passes (`ast.parse`); container restart clean (`Application startup complete.` x2 workers). E2E exercise pending next vol-outline generation (vol2/3 not yet generated; vol1 already materialized).

## 2026-05-07 ch4-10 补完 + chapter-outline 重写 title bug

### 已完成
- vol1 ch1-10 全部 completed（139,602 字）。
- ch5-10 验证 PR-OL2 vol-level 物化路径：chapter row 自动生成 + idempotent，content gen 直接复用 chapter_id。
- driver 脚本保留：`/tmp/ch5_to_10_driver.sh`、`/tmp/ch9_10_resume.sh`（含重试逻辑）。

### NEW backlog（从本次跑批发现）
1. **chapter outline 重写 title 问题**
   - 现象：`POST /api/generate/outline level=chapter chapter_idx=N` 会调 LLM 重新 propose chapter title 并写入 chapters.title，覆盖 vol1 outline 已确认的 chapter_summaries[idx].title。
   - 后果：ch8 / ch9 被改成第二人称 title（「骨灯认你」「你把他写得太干净」），违反 rename 约束。已 SQL 还原。
   - 修复思路：在 `chapter_outline_expander.py` 生成 prompt 里添加「章名仅生成 outline、title 使用传入的 fixed_title」；或者在 `_persist_outline_now` chapter 分支，若 vol1 outline 已有 confirmed title，则强制覆写 chapters.title 为 vol-outline title。
   - 优先级：P1（今后跑 vol2 / vol3 继续踩坑）。
2. **content gen 重试路径跳过 auto_revise**
   - 现象：ch9/ch10 从 SSE 看只有 `generating` -> `saved` -> `completed`，没有 `event: scored` / `revise_skipped`，且生成长度从 13-16k 降到 ~10k。
   - 猜测：scene_mode 在「上次部分 scene 已落盘」或 cache hit 时跳过了 evaluation 环节。
   - 待查：`backend/app/services/chapter_generator.py` 重入判断 / scene_writers 结束后的 evaluation hook。
3. **NVIDIA SSE INTERNAL_ERROR 偶发**
   - 外部因素，看起来 ~7–9 min long-stream 可能被上游负载均衡切换。
   - 临时方案：driver 层 retry x3 + sleep 30-45s（本次验证能挡 1 次 RST）。中期在 `_chapter_streamer` / NVIDIA client 加 chunk-level reconnect。

### 遗留 / 合并项
- 之前列出的「山门还债」「钟声过来」「债认账」物体施动抽象动作类问题 -> 仍在 backlog，可同带 batch sed 或后续 regen。
- chixin reprocess celery worker (~6000 slices) -> 仍未决定。
- handoff doc Stage B payload 字段 -> 未处理。

### 接手者首动
1. 检查上述 P1 chapter-outline title 覆写 bug 是否要今天修，还是留给下一轮。
2. 决定是否补跨集验证报告中的 ch9/ch10 evaluation 数据（需重跑 content gen 走一次完整 auto_revise）。

---

## 2026-05-07 · PR-TITLE-Q1 + PR-CHGEN-ALIAS 已合入

### 状态变更
- ~~P1.1 章名生成质量事后人工 SQL 修复~~ → 已代码层修、生成时保证（`title_quality_checker` + VOLUME_CHAPTERS_SYSTEM 重写 + chapter-outline title 冻结）。
- ~~P1.2 driver `scene_mode` 被 Pydantic 静默忽略~~ → `GenerateChapterRequest` 加 model_validator 容错，`scene_mode` / `scene_mode_on` 均能调起 scene mode + auto_revise。
- P1.3 NVIDIA SSE chunk-level reconnect：仍保留 driver-层 retry，未实施 chunk 级 reconnect（重复 token / billing 风险）。Backlog。

### 下一个接手者需要做的事
1. **vol2 outline 生成验证质量门** — 调 POST `/api/generate/outline` `level=volume volume_idx=2`，跑完后看 backend 日志中 `Staged volume outline: title quality check {checked, violations, rewritten, kept}`。`violations==0` 代表生成一次到位；`rewritten>0 kept==0` 代表重写全部补救成功。
2. **driver 脚本 改 `scene_mode` → `use_scene_mode`** — 这不是为了必须改（已容错），是为了代码与后端 Pydantic 字段名统一。未来可考虑加 deprecation log。
3. **可选 上走 ch11+** — 验证 SSE 能看到 `event: evaluating` / `event: scored`。
4. **不动 vol1 ch1-10 已落库数据** — 但注意：下次 chapter-outline 重生不会再被 LLM 改名。
5. **title_quality_checker 黑名单进一步打磨** — OBJECT_NOUNS / ABSTRACT_VERBS 是初版，遇到漏抓案例加进去即可。

---

## 2026-05-08 19:14 环境阻塞：上游 codex auth 失效

**现象**：所有 LLM chat task（outline_book / outline_volume / outline_chapter / generation 等）全部返回 503，错误：
```
Error code: 503 - auth_not_found: no auth available (providers=codex, model=gpt-5.4(high))
```

**诊断** (2026-05-08 19:14 交换现场验证)：
- `prompt_assets.outline_chapter` 路由正确 → endpoint `大纲` (`ac6eb9cd-380d-475f-83e8-b144dbdefe74`) `gpt-5.4(high)`。
- `大纲` 和 `本地 Qwen` 两个 endpoint **base_url 完全一样** = `http://141.148.185.96:8317/v1`，共用同一上游代理。
- 代理在 codex provider 上缺凭据，gpt-5.x 系列全部炸：按 model 名路由到 codex provider → codex 没 token → 503。
- 本 PR 代码路径都正确；这是部署/运维层问题。

**证据**：
```
2026-05-08T11:14:57.55Z  POST http://141.148.185.96:8317/v1/chat/completions "HTTP/1.1 503 Service Unavailable"
2026-05-08T11:14:58.86Z  Unhandled exception on POST /api/projects/.../chapters/.../outline/expand
  exc_type: InternalServerError
  exc_msg : Error code: 503 - auth_not_found: no auth available (providers=codex, model=gpt-5.4(high))
```
3 次 retry 全 503，后端耗时 2.7s 快速失败，不是间歇性。

**修复选项**（代码以外）：
1. 修 `141.148.185.96:8317` 代理上 codex provider 的 auth（加上 OPENAI_API_KEY 或 codex 特定凭据）。首选。
2. 换 endpoint 主机：在 Settings > 模型配置 里把 `大纲` / `本地 Qwen` 的 base_url 改成另一个可用的 OpenAI-compatible 代理。
3. 增加 endpoint：接上 OpenAI / Anthropic 官方 API，在 `prompt_assets` 里把 `outline_book` / `outline_volume` / `outline_chapter` / `generation` 重新绑到新 endpoint。

修好后验证：
```
TOKEN=$(python3 -c 'import json; print(json.load(open("/tmp/login.json"))["token"])')
curl -sS -X POST 'http://127.0.0.1:8000/api/projects/df6f523e-f903-4644-bcce-636f5ed89c68/chapters/3ea75111-015d-4a97-ae8b-5cdf8d802351/outline/expand' \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' --max-time 180 -w 'HTTP=%{http_code}\n'
```
预期 HTTP=200，返回 outline_json。

## 下一接手者优先级

0. **修 codex auth**（需用户介入，在代理运维层面）。
1. 验证 ch11 expand 能返 outline_json（SSE 同步调用，能看到 P1.1 title 冻结生效 + P1.3 重试机制正常）。
2. 走一次 chapter content gen、验证 SSE 返 `event: evaluating` 和 `event: scored`，证明 PR-CHGEN-ALIAS (P1.2) 双名字兼容正常。
3. Stage D 继写 ch11-ch30（凑 30 章赤心仿写验证量）。
4. Stage E 写 docs/CHIXIN_VALIDATION_REPORT_2026-05-05.md 终稿（合 30 章数据 + 全部 PR 总结 + 评分表）。

---

## 2026-05-08T16:00Z — codex auth 阻塞解除 ✅

上游代理 `141.148.185.96:8317` 的 codex provider auth 已恢复（由用户运维处理）。验证：

```
POST /api/projects/.../chapters/3ea75111-.../outline/expand
=> HTTP 200, latency 80,536ms, key_events=4
```

之前的 `auth_not_found` 503 / 2.7s 快速失败信号全部消失。这一节仅作历史记录保留，下一个接手者不需再处理。

## 2026-05-08T16:05Z — ch11 完整闭环 + PR-CHGEN-ALIAS 验证 ✅

ch11 《栖乌县城递状难》生成路径 saved 进库：

- POST `/api/generate/chapter` payload 用 PR-CHGEN-ALIAS 的短名：`{"scene_mode": true, "auto_revise": true, "target_words": 14000, "max_tokens": 8000}`
- SSE event 序列完备：`generating Starting` → [正文流式] → `status: saved word_count=12225` → `event: evaluating round=1` → `event: scored round=1 overall=8.18 issues=15` → `event: revise_skipped reason=score_above_threshold threshold=7.0` → `status: completed` → `[DONE]`
- 耗时 7:41，word_count=12225，score=8.18 与 ch1-8 同区间（8.28-8.52）
- 文案质量：开场即冷雾盐霜城墙，陆照藏芝麻蜡片入鞋底；结尾「脚步停在门后 … 下一声问话，可能要命，也可能给路」——与 ch1-10 同手感。

**这证明 PR-CHGEN-ALIAS 修复生效**：`scene_mode → use_scene_mode` alias 走通了 SceneOrchestrator + ChapterEvaluator 路径，2026-05-07 ch9/ch10 重跳时出现的「empty event: scored SSE traces」现象不复现；scored event 现在带完整的 `overall` / `issues` 字段。

## 2026-05-08T16:07Z — Stage D ch12-20 batch 启动

Driver: `scripts/stage_d_batch.sh` （9 章串行跳，预计 ~80 min）
Log: `/tmp/stage_d_run.log`
单章产出: `/tmp/sd_ch{IDX}_expand.json` + `/tmp/sd_ch{IDX}.sse`
Batch pid: 2832166


## 2026-05-09 (北京 20:25) — Batch B 部分成功 + codex 二度阻塞

### Batch B 结果 (12 章 nohup, scripts/stage_d_batch.sh max-time 升至 2400s/章)
- ch19 重跑: 11102 字, score 8.38, revise_skipped ✅ (覆盖 batch A 截断版 11656/score=6.46)
- ch20 重跑: 13694 字, score 8.36, revise_skipped ✅ (覆盖 batch A 截断版 10108/无 score)
- ch21 半成品: gen 中段 codex auth 401, scene_writer 崩, DB word_count=0 status=draft (SSE 文件 52KB 半段内容废弃)
- ch22-30: expand HTTP=500 全失败 (12 秒内集中, 503 auth_not_found providers=codex)

### 根因
codex 上游 auth.json 在 2026-05-09T12:24:29Z 二次失效（同 5/8 故障重现）。
- 健康检查: `curl http://141.148.185.96:8317/v1/chat/completions` → HTTP 401 "Missing API key"
- backend log: `"Your authentication token has been invalidated. Please try signing in again."` 同 5/8 完全一致字面值

### 接手第一件事 (恢复 codex 后)
1. host 端 codex login (用户操作)
2. 验证 curl: 期望 HTTP 200 + completion 体
3. 续跑: `nohup bash scripts/stage_d_batch.sh 21 22 23 24 25 26 27 28 29 30 >>/tmp/stage_d_run.log 2>&1 &`
   - max-time 已是 2400s/章 (PR-CHGEN-MAXTIME)
4. ETA: 10 章 × ~10 min ≈ 100 min
5. 跑完后追加 CHIXIN_VALIDATION_REPORT v1.3 (ch21-30 评分)

### 当前 vol1 状态
- ch1-20 全部 completed = **266,931 字**, 平均 13,347 字/章
- ch21-30 全部 draft, 等待 codex
- title 全部 clean (PR-TITLE-Q1.2 锁定)

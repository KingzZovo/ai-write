
> **✍️ 2026-05-27 v4.10 Chinese prose mechanics**：根据第一章人工阅读反馈，新增跨章节/跨项目通用中文行文机械约束：长短句呼吸、朴素准确动词、禁止生造动宾、空间动作符合物理常识、环境随视角流动、长段复述改为概括侧写、拒绝动作切片。该约束进入生成前 prompt / writer contract / force_direct 直出路径，用于第一稿内化，不作为后置 hard gate。
## 2026-05-27 v4.9 compact evaluator output
- The evaluator prompt now requires compact JSON: no prose excerpts, no Markdown, short descriptions/suggestions, and at most 8 real issues per dimension with high/medium issues prioritized.
- The chapter outline is serialized compactly in the evaluator input. This reduces scoring output/input overhead and is generic across chapters/projects.
- This does not introduce post-output blocking/repair and does not turn infrastructure failures into quality scores.


## 2026-05-27 v4.9 evaluator non-stream fallback
- Evaluation calls now default to non-streaming OpenAI-compatible chat completions (`EVALUATION_LLM_STREAM=0`) to avoid stalls in streaming iterators on slow/local scoring endpoints.
- Scoring output budget is reduced by default (`EVALUATION_LLM_MAX_TOKENS=2048`, temperature `0.2`) while retaining hard request timeout, tier fallback, and failed-task semantics for infrastructure failures.
- This is scoring infrastructure only; it does not add post-output gates or generation-time repair.


## 2026-05-27 v4.9 evaluator infrastructure failure semantics
- Fixed evaluator failure semantics: model timeout/fallback failure and invalid evaluator JSON now raise `EvaluationInfrastructureError` instead of returning a synthetic all-zero `EvaluationResult`.
- Async evaluation tasks persist these cases as `failed`, so downstream exports do not treat infrastructure outages as chapter-quality scores.
- Removed the bogus v4.9 `overall=0/issues=1` row generated while validating the timeout guard; v4.9 remains generated/saved but not validly scored until evaluator infrastructure returns a real rubric result.


## 2026-05-27 v4.9 evaluator fallback timeout refinement
- After the first saved-draft evaluation proved the timeout guard works (failed cleanly with `TimeoutError()` and no stuck Celery task), evaluation routing was refined to avoid spending the whole task timeout on one endpoint.
- Evaluation OpenAI-compatible provider calls now default to one provider retry attempt, letting `ModelRouter.generate_with_tier_fallback` move to another configured endpoint/model after a bounded timeout.
- Evaluation request timeout defaults to 55s and the async evaluation task hard cap defaults to 210s, preserving a hard upper bound while allowing bounded tier fallback.
- This remains infrastructure safety/scoring reliability only; it does not add post-output quality gates or auto-revision to the generation path.


## 2026-05-27 v4.9 evaluator timeout guard
- Added reusable LLM timeout hardening after v4.9 evaluator stuck following upstream HTTP 500.
- `OpenAIProvider` now disables long SDK retries by default and applies request-level timeouts.
- `llm_retry.call_with_retry` supports `per_attempt_timeout`.
- `ChapterEvaluator` passes an evaluation-specific request timeout.
- Async `evaluate_chapter_task` uses a hard wait_for and records failure instead of retrying indefinitely.
- This is infrastructure safety only; it does not reintroduce post-output hard gates or auto-revise as the main generation path.

> **⚙️ 2026-05-27 v4.9 micro-continuity budget**：第五章 v4.8 直出验证未达标（overall 7.24 / issues 19），主要复发为 power_resource / expression / information / result_strength / space 等生成前约束未充分内化。v4.9 不引入 hard gate、needs_repair、post-output blocking 或 auto_revise 主流程，也不回到 scene_planner；继续 force_direct，并把 v4.8 的 anchor_audit_before_prose 升级为每个 outline_execution_unit 的 `micro_continuity_budget`：`unit_movement_budget`、`unit_resource_budget`、`unit_information_ladder`、`unit_expression_role`、`unit_result_delta_cap`，要求正文生成前完成预算扣账，预算缺失时只能降级为疑点/残片/待验。

> **✅ 2026-05-22 10:55 动作预算与推理台账通用约束**：针对第三章反复出现 time / power_resource / mechanism / information / result-strength 阻断标签的共同根因，继续做题材无关机制级修复：在叙事合同、章节大纲扩写、ContextPack 渲染、场景规划、正文写作和自动修订根因蓝图中加入 `action_budget` / `inference_ledger`。现在 scene_planner 输出缺少高压动作预算或推理台账会被 `_has_valid_scene_contract` 拦截并 fallback；fallback scene 也会填充两个字段；writer content 显式带入【动作预算】/【推理台账】。验证使用 compileall、直接 smoke、git diff --check 与 health check；不以当前 pytest 环境作为通过依据。下一步基于新合同重做/重修第三章，不再在 v5 正文上堆补丁。

> **✅ 2026-05-22 19:52 Auto-revise 失败回滚（防止坏稿落地）**：根治“阻断标签/超时/轮次耗尽后，仍把低分坏稿写进 chapters.content_text 成为 active”的工程路径：在 `/api/generate/chapter` 的 C2 auto-revise loop 增加 baseline snapshot + rollback。只有当 `should_revise(final_eval)==False`（≥阈值且无阻断标签）才视为 accepted；否则一旦出现 `root_cause_repair_required`、`timeout/empty`、save 失败或 loop 异常，就自动把 `chapters.content_text` 回滚到生成前基线并标回 `draft`，同时 SSE 发出 `rollback_applied` 事件。这样后续同类问题不再污染主表正文，必须先做上游根因修复/级联修复再推进。

> **✅ 2026-05-21 18:55 场景合同缺字段真实拦截**：继续排查第三章反复出现 time/space/information/mechanism 等阻断标签的根因，发现此前 `_has_valid_scene_contract` 只定义和测试了纯函数，`plan_scenes` 实际未调用，导致缺少连续性台账/时间空间实体字段的 scene 规划仍可能进入正文写作。已在 `SceneOrchestrator.plan_scenes` 增加真实拦截：只要任一 scene 缺 start_state / time_delta / location_path / entity_transfers / power_resource_map / information_state / mechanism_limits / result_strength / transition_bridge / continuity_ledger，就记录缺失字段并 fallback 到带通用台账的保守规划；新增回归测试覆盖缺字段规划必须 fallback。compile / diff check / 直接 smoke / health 均通过。第三章人工复评仍未达标，后续应走机制链路重生/规划修复，而不是继续手工抽卡式改正文。

> **✅ 2026-05-21 16:10 通用连续性台账与反抽卡 gate**：按 King 纠偏，停止围绕第三章具体症状打补丁，改为机制级通用修复：scene_planner 必须产出题材无关的 `continuity_ledger`，并与 start_state / entity_transfers / transition_bridge 一致；缺少时间、空间、实体、信息、机制、结果强度、连续性台账字段的 scene 规划会被拦截并 fallback。scene_writer 强制消费本场结构化台账，auto_revise 对 time/space/information/mechanism/power/result-strength 阻断标签即使分数达标也不放行；同类阻断标签跨轮重复时停止整章随机重抽，返回根因修复蓝图。已补场景合同与反抽卡单测，compile / diff check / 纯函数 smoke 通过；完整 pytest 受当前 venv 缺 `bcrypt` 阻断，未作为通过依据。

> **⚠️ 2026-05-20 22:48 章节大纲长事务修复**：第 3 章大纲扩写在 LLM 返回后写库时触发 Postgres `idle-in-transaction timeout`，根因是章节大纲扩写 API 在等待上游模型期间一直持有读事务。已改为上下文读取完成后先释放事务，模型返回后重新取 fresh chapter 再写入，避免长时间 LLM 调用导致连接被数据库关闭。

> **⚙️ 2026-05-20 22:20 大纲链路上移世界逻辑合同**：确认章节偏离不是正文单点问题，已把题材无关 World Logic Contract 前移到全文大纲、分卷大纲、章节摘要和章节大纲扩写；章节大纲新增 start/end state、time_delta、location_path、entity_transfers、information_state、power_resource_map、mechanism_limits、result_strength、handoff_to_next 等可执行状态字段。ContextPack 显式渲染卷/章合同字段，cascade planner 对 time/space/power/information/mechanism/result-strength 违规优先回写 chapter.outline_json；无 outlines 行时不再跳过，而是落到章节大纲修复。

> **✅ 2026-05-20 13:45 世界逻辑合同改造**：按 King 纠偏，不再把约束写死到某章、某本书或某个题材；新增题材无关的 World Logic Contract，要求生成前先从项目题材/设定/大纲/已生成章节推导时间、空间、权力/资源、信息、能力/机制、结果强度六类规则。ContextPack 全局注入该合同，scene_planner 输出场景合同字段，scene_writer 只能按结果强度渲染，chapter_evaluator 按合同违规硬验收，auto_revise 按违规类型修复而非症状补丁。

> **✅ 2026-05-19 21:05 赤心巡天完成巡检**：定时巡检确认《赤心巡天》decompile retry 已到 `ready`，style/beat 均 `22941/22941`，缺失与重复为 0；抽样内容非空且无乱码/截断。同步修复少量 LLM `items` / `scenes` 包装 JSON 对下游顶层字段读取的影响，并补回归测试与 RUNBOOK 对账 SQL。

> **✅ 2026-05-19 10:48 代码巡检优化**：按 King 指令巡检项目代码与创建/生成链路；确认前端已无错误参考书路由残留，后端 compile 与 `/api/health` 正常。顺手修掉 `GeneratePanel` 写法/架构选择器的 TS/ESLint 问题：去掉无用参数与 `any`，补齐结构类型，避免 effect 内同步 setState 报错，并同步移动端调用签名，确保 `npm run build` 通过。

> **✅ 2026-05-19 08:42 参考书补齐**：按 King 指令补齐《天之炽》《天之炽②女武神》的 `plot_structure` / `plot_structure_v2`，并用 style_v9 补强两本单书综合写法画像；随后验证江南三本（龙族、天之炽、女武神）卡片覆盖、结构字段、画像规则数与 prompt 编译。
> **⚙️ 2026-05-18 18:58 动态提速**：runtime=15 下 beat 仍在推进但受上游 `auth_not_found` 波动拖慢；按 King 指令将 `.env` 的 `REFERENCE_INGEST_CONCURRENCY` 从 `15` 上调到 `20`。继续保留同书 single-flight 与 retry 保活机制，等待无 active wave 的窗口重建 worker 并自动补派。
> **⚙️ 2026-05-18 15:05 动态提速**：`赤心巡天` style 已补齐，当前只剩 beat 分支；按 King 指令将 `.env` 的 `REFERENCE_INGEST_CONCURRENCY` 从 `10` 上调到 `15`。保留 Celery 同书 single-flight，等待当前活跃 wave 完成后安全重建 worker，避免中断正在推进的补齐任务。
> **⚙️ 2026-05-18 11:45 retry 保活修复**：按 King 要求，`retry_reference_book_missing_branches` 在 0 增量 wave 连续失败时不再到 `max_auto_retries` 后停止；改为短延迟继续重试，并将 attempt 作为观测计数循环回 1。single-flight lock 仍防止同书重复 wave 堆积，确保上游 429/503/auth 故障期间也持续探测直到 `ready`。
> **⚙️ 2026-05-17 22:40 动态提速**：runtime=8 已生效超过 1 小时，期间队列无堆积、未见 redelivery / connection closed，`赤心巡天` 进度继续上涨；19:32→22:38 beat 从 `11079` 到 `11779`，约 `+700`，折算约 `220+/h`，较先前 runtime=5/6 观察值有提升。按 King 指令将 `.env` 的 `REFERENCE_INGEST_CONCURRENCY` 从 `8` 上调到 `10`，并安排当前活跃 wave 完成后安全重建 Celery worker。

> **⚙️ 2026-05-17 19:35 动态提速**：runtime=6 生效后，除一波受 codex/auth 上游影响只有部分增量外，后续连续多波恢复 `50/50`，未见 redelivery / connection closed / 队列堆积；按 King 要求将 `.env` 的 `REFERENCE_INGEST_CONCURRENCY` 从 `6` 直接上调到 `8`，并安排当前活跃 wave 完成后安全重建 Celery worker。

> **⚙️ 2026-05-17 17:05 动态提速**：runtime=5 重建后连续多个 retry wave 正常完成并保持 `style_filled=50`、`beat_filled=50`，wave 耗时从约 20.7min 降至 13.8–22.8min，未见新的 redelivery / connection closed；按 King 规则将 `.env` 的 `REFERENCE_INGEST_CONCURRENCY` 从 `5` 上调到 `6`，并安排当前活跃 wave 完成后安全重建 Celery worker。

> **⚙️ 2026-05-17 15:25 retry 策略调整**：按 King 纠偏，LLM 上游 429/503/auth 短暂故障不再按长指数退避处理；`retry_reference_book_missing_branches` 在 0 增量 wave 后改为 `DECOMPILE_RETRY_STALL_DELAY` 短延迟（默认 60s）继续重试，仍保留同书 Redis single-flight lock，避免重复 wave 堆积。

> **✅ 2026-05-17 15:00 创建链路纠偏**：确认项目链路应选择并调用“写作风格 StyleProfile + 剧情架构 plot_structure”，而不是在新建项目直接选择参考书，也不是把全书大纲路由改到 `/api/outlines/from-reference/start`。已撤回错误的直接参考书入口，并补齐 `/api/generate/outline` / `/api/generate/async` 及异步 worker 对 `settings_json.style_reference.profile_id` 与 `settings_json.plot_structure.structure_book_id` 的后端兜底解析，剧情架构优先使用已抽取的 `metadata_json.plot_structure`；创建项目只保存 `style_reference.profile_id`、`style_profile_id`、`plot_structure.structure_book_id`。

> **⚙️ 2026-05-17 12:00 动态提速**：小时巡检显示并发 `4` 下最近 4 个 retry wave 均稳定完成（约 1403–2002s/波），每波 `style_filled=50`、`beat_filled=50`，未见 visibility/redelivery/duplicate/lock 挤压；按规则把 `.env` 的 `REFERENCE_INGEST_CONCURRENCY` 小步上调到 `5`。注意：运行中的 Celery worker 当前仍显示 env=4；为避免打断当前活跃 retry wave，本次不强制重启，等下次安全维护窗口/自然重启后生效；下一小时继续观察失败率与 wave 耗时。

> **⚙️ 2026-05-17 09:30 动态提速**：按 King 指示开始小步上调《赤心巡天》补全吞吐：`REFERENCE_INGEST_CONCURRENCY` 从 `3` 调到 `4`（Celery worker concurrency 暂不动，仍依赖同书 single-flight）。后续每小时巡检观察 wave 耗时、`style_filled/beat_filled`、APIError / cooldown / auth 失败率；若失败率没有大幅增加则继续小幅上调（优先 4→5，再评估 `DECOMPILE_RETRY_WAVE_BATCH` 50→75），若失败显著增加或 wave 接近/超过 visibility window 则回退。

> **⚠️ 2026-05-17 04:00 巡检修复**：`retry_reference_book_missing_branches` 在 single-flight 后又因长波持有 snapshot AsyncSession 触发 Postgres idle timeout（`connection is closed`），导致队列为空、补全停止；已改为波前关闭 snapshot session、波尾 fresh session 对账并补发 retry。详见 [docs/HANDOFF_2026-05-17_retry-session-reopen.md](HANDOFF_2026-05-17_retry-session-reopen.md) 与 RUNBOOK §0.1。

> **⚠️ 2026-05-17 01:00 巡检修复**：`retry_reference_book_missing_branches` 出现同书 4 个 redelivered 并发任务；已加 Redis single-flight lock（`decompile_retry:lock:{book_id}`）并把默认 `DECOMPILE_RETRY_WAVE_BATCH` 从 250 降到 50。详见 [docs/HANDOFF_2026-05-17_retry-singleflight.md](HANDOFF_2026-05-17_retry-singleflight.md) 与 RUNBOOK §0.1。

> **⚠️ 2026-05-15 23:31 最新交接**：PR-A-GEN-PIPELINE-FIX 已 push 到 `feat/pr-a-gen-pipeline-fix`（`cd77b5c` + `8583910`）**但尚未合 main**。Bug A/B/C 三连修：batch_generator 签名发送 / chapters 表持久化 / PUT empty-content protect guard。接手看 [docs/HANDOFF_2026-05-15_pr-a-gen-pipeline-fix.md](HANDOFF_2026-05-15_pr-a-gen-pipeline-fix.md)。本页以下内容为历史。

> **✅ 2026-05-06 07:20 最新交接**：PR-BOOK-PROFILE-BIND **已完成**（commit `f73a74d`，pushed `origin/main` + `origin/feat/phase2-fix`）。完整执行日志 + P0-LOCK 复盘看 [docs/HANDOFF_2026-05-05_pr-book-profile-bind.md §12-13](HANDOFF_2026-05-05_pr-book-profile-bind.md)。下一步：Stage B 创建赤心仿写验证项目（绑 `b76da43a-…` profile + `0a543b1d-…` reference_book）→ Stage C 全书/分卷/章节大纲 → Stage D bulk_generate 10 章 → Stage E `docs/CHIXIN_VALIDATION_REPORT_2026-05-05.md`。本页以下内容为历史。

# 项目当前进展（持续维护）

> **2026-05-04 14:50 交接**：B1 收尾·C 完成·D 用户补、E 丢弃。**PR-OUTLINE-DEEPDIVE 四阶段全完成**：
> Phase 1 · chapter_outline_expander LLM 服务 + API；Phase 4 · 本章大纲中文结构化进 prompt；Phase 2 · OutlineEditor AI 扩写按钮；Phase 3 · raw_text 修复脚本（dry-run 烟测通过）。
> 详看 `docs/HANDOFF_2026-05-04_b1+outline-deepdive-done.md` 与 `docs/PR-OUTLINE-DEEPDIVE_2026-05-04.md`。
> 上一窗口 banner：phase2-fix B1 批次 9 commits · 看 `docs/HANDOFF_2026-05-04_phase2-fix.md`。

> 目的：让任何新窗口/新同学**只看这一份**就能知道：已经做了什么、现在做到哪、下一步做什么、怎么验收。

## 0. 一句话架构结论（TL;DR）

- **Neo4j 是设定集真相源（source of truth）**；Postgres 仅为读优化投影（materialize）。
- 任何设定集实体写入：**写 Neo4j → materialize → PG**；禁止 PG 直写以避免 drift。
- ✅ **`foreshadows` 已纳入 Neo4j 真相源链路**（F1=A 落地，PR #18 合入），foreshadows API 与 `foreshadow_manager.create` 改走 `POST /api/projects/{pid}/neo4j-settings/foreshadows`。
- ✅ **`/neo4j-settings/*` 与 `/admin/entities/materialize` 端点已在 main 实现**（PR #18 合入）。
- 当前可用入口：`POST /api/projects/{pid}/outlines/{oid}/extract-settings`（提取链路）+ `POST /api/projects/{pid}/neo4j-settings/{characters|world_rules|relationships|locations|character_states|organizations|foreshadows}` + `POST /api/admin/entities/materialize`。详见 RUNBOOK §1。

## 1. 最近一次更新

- 日期：2026-05-02（晚）
- 更新人：自动执行代理（GitHub MCP + AWS MCP shell @ `/root/ai-write`）
- 关联 PR：#8 - #18（v1.0 → v1.9 共 11 个 release PR 全部合入）
- 本地状态：`main` HEAD = `0a4f9a1`（PR #18 合并 commit），working tree clean
- 远端状态：`origin/main` HEAD = `960a38b`（PR #19 合并）；P6 仓库清理已完成（仅剩 `main` + `archive/feature-v1.0-big-bang` tag）

## 2. 已完成（按时间倒序）

### 2026-05-02 晚 (E2E 全业务验证 + chapter generate-stream 签名修复)

**场景**：全链路 E2E 跑一遇 — 拆分参考小说 → 提取/蒸馏 → 向量化/入库 → 创建小说 → 书级大纲 → 分卷大纲×3 → 章节大纲×30 → 生成 30 章正文。

**项目**：`0eaeff87-2f91-452c-812c-b4bcf2924fe2` (《城下听潮》×仿《龙族》中二都市风)

**阶段结果**
| 阶段 | 状态 | 产出 |
|---|---|---|
| P1 参考书预处理 | ✅ | 3 本 status=ready (《龙族全套》/《天之炽》/《天之炽②女武神》（江南）)、qdrant 多维 collection 全点亮 |
| P2 创建项目 | ✅ | PID 如上 |
| P3 书级大纲 | ✅ | OID `f9af7582...` 6766 chars、is_confirmed=1 |
| P4 分卷大纲 ×3 | ✅ | TID 3个、并发 60-70s、章节摘要 JSON 30×{title,summary,key_events} |
| P5 物化 PG | ✅ | 3 volumes + 30 chapters、每章携 outline_json |
| P6 生成 30 章正文 | ✅ | **需修复** chapter generate_stream 签名 bug (见下) 后 30/30 全过 |

**Chapter 产出**：30/30 done，277,882 字，avg 9263，min 7377，max 13155；单章耗时 178~280s（4-worker 并发 ≈27 min 跑完 30 章）。抽样 v1.c1 / v2.c5 / v3.c10 剧情连贯、人物（林渡、乔野、闻栖枝）跨卷一致、语言风格合「中二热血×现代都市」。

**Bug fix (PR #21)**：`backend/app/tasks/knowledge_tasks.py` 调用 `ChapterGenerator.generate_stream` 仍按 v0.4 签名传 `project_settings/world_rules/...`，与 v0.5+ 新签名（`*, project_id, volume_id, chapter_idx, db, chapter_id, user_instruction`）不匹配，导致所有 `task_type=chapter` 的 celery 任务 0.06s 立刻 TypeError。patch +9/-3，合并后 E2E 过。

**后续（未起）**
- 章节生成未自动触发 entity 抽取/cascade/evaluation（`characters=0, foreshadows=0, cascade_tasks=0`），如需可手动调用 `entities.extract_chapter` / `evaluations.evaluate_chapter` celery task 或接入生成后钩子。
- 建议加 `tests/tasks/test_run_async_generation_chapter.py` mock ChapterGenerator 锁定签名。

### 2026-05-02 — `feature/v1.0-big-bang` 237 commit 全量回收（11 个 release PR）

按版本号干净拆分，每个 PR 独立 cherry-pick + 解决冲突 + compileall + push + open + merge：

| PR | branch | range | commits | 关键内容 |
|---|---|---|---:|---|
| #8 | `release/v1.0.0` | `036d9a0..99170ed` | 20 | Docker / Sentry / Prometheus / GH Actions CI / BVSR / LangGraph / usage_quotas / EPUB-PDF-DOCX / i18n / mobile |
| #9 | `release/v1.1.0` | `99170ed..e35cd90` | 5 | en i18n / mobile landing / design tokens / sidebar memory |
| #10 | `release/v1.2.0` | `e35cd90..37e5379` | 5 | JSON logging / X-Request-ID / Prometheus 扩展 / Sentry redaction / CI smoke |
| #11 | `release/v1.3.0` | `37e5379..f1f4730` | 6 | target_word_count + budget allocator + cascade |
| #12 | `release/v1.4.0` | `f1f4730..cdc6d2b` | 20 | LLM tier routing |
| #13 | `release/v1.4.1` | `cdc6d2b..2aedd2a` | 7 | probe surface + max_tokens + staged outline |
| #14 | `release/v1.5.0` | `2aedd2a..581f957` | 61 | chunker fix + tier-aware fallback + scene mode + cascade tasks |
| #15 | `release/v1.6.0` | `581f957..1d8d53b` | 5 | prompt cache + scene_mode metrics |
| #16 | `release/v1.7.x` | `1d8d53b..1b455b9` | 19 | cascade panel + time_llm_call + outline injection + post-summarizer + anti-AI prompts |
| #17 | `release/v1.8.x` | `1b455b9..7209960` | 8 | dosage anti-AI + Bug L 自动保存 |
| #18 | `release/v1.9.0` | `7209960..73e7897` | 81 | **Entity writeback Neo4j↔PG + F1=A foreshadows-via-Neo4j** |

冲突解决记录：`.gitignore`（PR #8）+ `README.md`（PR #12）+ `docs/RUNBOOK.md`（PR #16/#18）+ `outline_to_facts.py`（PR #16）+ `entity_tasks.py`（PR #14）。PR #18 用 `cherry-pick -X theirs` 自动取 big-bang 版本。

### 2026-05-02 — F1=A foreshadows-via-Neo4j 落地（PR #18 包含）

关键 commit：
- `02a5f19` neo4j foreshadows write + materialize to postgres
- `cd8ed7a` make foreshadows api write neo4j and materialize pg
- `e16c492` make foreshadows resolve/delete write neo4j
- `a86738c` materialize foreshadows deletion to postgres
- `49776e0` route foreshadow writes to neo4j source-of-truth

现状：
- `backend/app/api/foreshadows.py`、`backend/app/services/foreshadow_manager.py` 不再 PG 直写，全部走 `/neo4j-settings/foreshadows`。
- 外部 API URL 兼容（前端无需改）。
- materialize 函数：`backend/app/tasks/entity_tasks.py` 的 `_materialize_foreshadows_to_postgres()`。

### 2026-05-02 — F2 v1.10 路由族提前落地（PR #18 包含）

- ✅ `backend/app/api/neo4j_settings.py` 已在 main（characters / world_rules / relationships / locations / character_states / at_location / organizations / membership / foreshadows）。
- ✅ `backend/app/api/admin_entities.py` 已在 main（`POST /api/admin/entities/materialize`，env `ADMIN_USERNAMES` JWT-sub gate）。
- ✅ `backend/app/api/admin_usage.py` 同步入仓（`/api/admin/usage`）。

### 2026-05-02 — alembic head 推进至 `a1001908`

新增迁移：
- `a1001200_v10_usage_quotas`
- `a1001400` LLM tier routing（v1.4）
- `a1001401_v141_prompt_max_tokens`
- `a1001900_v190_*` 系列：characters_unique / relationships_unique / world_rules_unique
- `a1001904_v190_locations_table`
- `a1001905_v190_character_locations_table`
- `a1001906_v190_character_states_table`
- `a1001907_v190_organizations_table`
- `a1001908_v190_character_organizations_table`（current head）

部署前必须 `alembic upgrade head`。

### 2026-05-01 ~ 2026-05-02 早 — 此前已合入（保留摘要）

- PR #1 ~ #7：PR #1 v1.9 主要收敛 / PR #2 文档 / PR #3 legacy 410 / PR #4-#6 HANDOFF + RUNBOOK + verify 脚本 / PR #7 FOLLOW_UP_PLAN 决策辅助文档
- 本地状态清理（P0）：丢弃未提交 settings.py，drop stash，pull --ff-only
- 防回归扫描（P2）：远端 search_code + 本地 grep 两路扫描 PG 直写 = 0 命中（除已知 foreshadow 3 处，现已修复）

## 3. 当前未决事项 / Follow-up

### F3 P3 part 3：真实环境对账（仅本机环境可执行）

- 脚本：`scripts/verify_entity_writeback_v19.sh`（PR #6 入仓）+ `scripts/verify_entity_writeback.sh`（PR #18 入仓）
- 命令：`PROJECT_ID=... CHAPTER_IDX=... bash scripts/verify_entity_writeback_v19.sh`
- 前置：后端启动 + PG/Neo4j 连接通 + 真实 PROJECT_ID
- 输出：legacy 410 烟测 / extract-settings 路由探测 / PG 行数 / Neo4j 对账

### 部署 checklist

- [ ] `alembic upgrade head`（target = `a1001908`）
- [ ] 配置 env `ADMIN_USERNAMES`（JSON 数组，例 `["admin","king"]`），否则 `/api/admin/*` 路由族对所有 JWT-sub 返回 403
- [ ] 验证 v1.9 entity writeback 全链：写 `POST /api/projects/{pid}/neo4j-settings/foreshadows` → Neo4j `(:Foreshadow)` 节点出现 → PG `foreshadows` 表 materialize 出同一行
- [x] **仓库清理完成**（2026-05-02 晚）：origin 上仅剩 `main` + `archive/feature-v1.0-big-bang` tag（锁 `73e7897` 237 commit 历史）。删除：feature/v1.0-big-bang + 11 个 release/v1.* + 4 个 doc/fix 分支 + 3 个 chore 分支

### 后续大版本（参考 ITERATION_PLAN.md）

- v0.6 ~ v1.0 之间的大版本规划见 `ITERATION_PLAN.md` v0.6/v0.7/v0.8/v0.9/v1.0 章节（设计文档 `docs/V06_DESIGN.md` ~ `docs/V10_DESIGN.md`）。
- 现在 `feature/v1.0-big-bang` 上的内容（v1.0 - v1.9）已实质落地到 main，主要从 v1.0 → v1.9 横跨多个 design 文档主题。后续若要继续推进 v2.0+，从 `ITERATION_PLAN.md` 的 Iteration 系列继续。

## 4. 验证命令清单（可复制粘贴）

```bash
cd /root/ai-write

# 仓库静态健康
git log -1 --oneline                                    # expect: 0a4f9a1 Merge PR #18
python3 -m compileall -q backend/app && echo OK         # expect: OK
ls backend/alembic/versions/ | grep -c '^a100'          # expect: 12+

# v1.9 关键文件 blob hash 与 big-bang HEAD 73e7897 一致
for f in backend/app/api/foreshadows.py \
         backend/app/api/neo4j_settings.py \
         backend/app/api/admin_entities.py \
         backend/app/api/admin_usage.py \
         backend/app/services/foreshadow_manager.py \
         backend/app/services/usage_service.py; do
  if [ "$(git rev-parse main:$f)" = "$(git rev-parse 73e7897:$f)" ]; then
    echo "OK $f"
  else
    echo "DIFF $f"
  fi
done

# 真实环境对账（需要 PROJECT_ID + 后端运行）
PROJECT_ID=... CHAPTER_IDX=... bash scripts/verify_entity_writeback_v19.sh
```

## 5. 历史关键 PR 一览

| PR | 合并 SHA | 主题 |
|---|---|---|
| #1 | `cfbdbf4` | v1.9 主要收敛（outlines extract / world_rules ETL / relationships deletion sync） |
| #2 | `3195927` | README + ITERATION_PLAN 文档持续维护 |
| #3 | `3c9f4b0` | legacy PG 直写接口禁用（→ 410） |
| #4 | `ca96d2c` | HANDOFF_EXECUTION 模板 |
| #5 | `17fb371` | PROGRESS 模板 |
| #6 | `8e34c0b` | RUNBOOK + verify_v19 + .gitignore + README/410 修正 |
| #7 | `ea624e2` | FOLLOW_UP_PLAN.md 决策辅助 |
| #8 | `144e84e` | release/v1.0.0（20 commit） |
| #9 | `444d6bf` | release/v1.1.0（5 commit） |
| #10 | `8ea639a` | release/v1.2.0（5 commit） |
| #11 | `3a7eca1` | release/v1.3.0（6 commit） |
| #12 | `0d0d0b7` | release/v1.4.0（20 commit） |
| #13 | `4c6bbf4` | release/v1.4.1（7 commit） |
| #14 | `bb364a5` | release/v1.5.0（61 commit） |
| #15 | `31b4875` | release/v1.6.0（5 commit） |
| #16 | `0f79975` | release/v1.7.x（19 commit） |
| #17 | `b54159d` | release/v1.8.x（8 commit） |
| #18 | `0a4f9a1` | release/v1.9.0（81 commit, F1=A） |

## feat/outline-batch2 — 7-PR 批次 ✅ 已 push（2026-05-03）

> 分支 `feat/outline-batch2`，HEAD = `f3e9e55`，自 main `1b52952` 起新增 9 个 commit（2 baseline + 7 PR）。
> 背景：上一轮 E2E PID `310c1f9a` 《狩人账》30 章跡走后朱雀 AI 检测 12.04% 人工 / 42.21% 疑似 / 45.75% AI，六个架构问题取得共识后拆出本批 7 PR 修正。

### Commit 链

| commit | PR | 主题 |
|---|---|---|
| `bf1ae1e` | baseline backend | 抢救 PR-OL1~9 backend 工作区改动（12 文件 +942/-16）|
| `de6d623` | baseline frontend | 抢救 fallback 卡片 / cascade UI / i18n（8 文件 +989/-599）|
| `70706c9` | **PR-OL10** | 字数→章数→卷数自动推算（默认 4000 字/章、100-200 章/卷）+ prompt 硬约束注入 |
| `e838cd6` | **PR-OL11** | 分卷大纲 chapter_summaries 强化（60-100 字 + 主线/支线/伏笔/关键场景）+ `extract_chapter_breakdown()` helper |
| `4b515ba` | **PR-OL12** | 章节大纲调用层补 `previous_chapter_summary` + 本章预规划注入 |
| `3d07194` | **PR-OL13** | 章节大纲生成后解析 `title` 回写 `Chapter.title`（清除「第N章」占位）|
| `f6fa9e5` | **PR-OL14** | OutlineTree 三层查看入口（全书/分卷/章节大纲）|
| `919abab` | **PR-AI1** | 命名与词汇硬约束：`FORBIDDEN_HALLUCINATION_TERMS` + `NAMING_DIRECTIVE` + context_pack 注入 |
| `f3e9e55` | **PR-STY1** | style v9 5 条节奏/留白/信息密度/句式/在场 directive + context_pack 注入 |

### Verification

- 所有 backend 改动过 `python3 -m py_compile`。
- 所有 frontend 改动过 `cd frontend && npx tsc --noEmit -p tsconfig.json`。0 错误。0 警告。
- 行为级 E2E 验证 + 朱雀复测 **延后** 到本批全部落定后一次跑完成，避免每个 PR 都付 SSE 长任务成本。

### 重跑测试预计步骤

1. 则使用 PID `310c1f9a` 清 30 个 Chapter / 30 个 chapter outline / 9 个已生成正文（保留 book outline + 20 个 volume outline 可复用）或新建项目。
2. POST `/api/projects` `target_word_count: 2000000`，走完全书 outline + 各卷 volume outline + 全 30 章 chapter outline + 30 章正文，验证：
   - 全书大纲 「七、分卷规划」 应说 「下输出 3-5 卷」（PR-OL10）。
   - 各 volume_outline.chapter_summaries 每项含 main_progress / side_progress / foreshadow_state / key_scene（PR-OL11）。
   - 各 chapter outline 调用 应额外携 previous_chapter_summary（PR-OL12，看 backend log）。
   - Chapter.title 不再是 「第 N 章」（PR-OL13，查 DB）。
   - 前端侧栏 OutlineTree 顶部能展开 「全书大纲」，每章能展开 「大纲」（PR-OL14）。
   - 生成的正文 grep 不到 「怎表」/「屃门」/「黄铜怎表」类含词（PR-AI1）。
   - 生成的正文节奏合理，段落长短交错，周期出现 1-2 句短段（PR-STY1）。
3. 取 V1 CH2 等价隐藏位置贴朱雀 AI 检测，取人工/疑似/AI 三段比例与 baseline 12.04% / 42.21% / 45.75% 对比。

### Neo4j 状态机扩展状态（不在本批，居后动工）

| 维度 | 现状 | 缺口 |
|---|---|---|
| 地点 | ✅ 已实现 `Location` 节点 + `AT_LOCATION` 关系（chapter_start 时序）| 无 |
| 阵营 | ⚠️ 半：有 `Organization` 节点 + `MEMBER_OF`，没有 「阵营事件」 | 缺 `FactionEvent`（结盟/破盟/开战/休战）|
| 道具 | ❌ 未实现 | 缺 `Item` 节点、`HAS_ITEM`/`USES_ITEM` 关系、prompt 不抽 `items` |
| 时间 | ❌ 未实现（仅 chapter_start 隐式时序）| 缺 `Time`/`Era`/`TimeEvent` 节点、`OCCURS_AT` 关系 |

列为下一批 PR-NEO1~NEO4 开新分支 `feat/neo4j-batch1`。


---

## 2026-05-03 V2 + V3B 实证报告 + 修订 Plan（**唯一权威 plan**）

> 取代 `FOLLOW_UP_PLAN.md` / `HANDOFF_TODO.md` / `HANDOFF_2026-05-03_outline-batch2.md` 的 PR 排序。后续 plan 演进只在本节追加，不开新文件。

### 1. V2 跑（200 万字、5 卷、staged_stream + 并发卷大纲，PID `20d164ab-232f-4863-8265-452186638d83`）

| 阶段 | 结果 | 备注 |
|---|---|---|
| A 建项目 | ✅ 3 s | `target_word_count=2000000` |
| B 全书大纲 | ✅ 7 m 04 s | book_oid `bf1b3cf1…`，**PR-OL10 仍失效**：volume_plan 输出 5 卷而非 3-5 |
| C1+C2 建 5 卷壳 | ✅ 4 s | 每卷 `est_chapters=150`、共 750 章 / 2 M 字 ≈ 2 667 字/章 |
| D 5 卷大纲并发 | ⚠️ **3/5** | vol 2/3/4 OK；vol 1（外滩怀表）/ vol 5（红玉无声）SSE 中断 fail |
| E `outline_to_facts.run_full_etl` | ❌ | inline `python -c` 内 `async def` 跟在 `;` 后 → SyntaxError |
| F 自动建章 | ❌ | `volumes.py` SSE handler `json.loads` 失败兜底 `{"raw_text": full}`，下游 `parsed.get("chapter_summaries") = None` → 0 章 |

**关键 bug PR-VOL2-PARSE**：`backend/app/api/volumes.py:217` 的 `try: parsed=json.loads(cleaned) except: parsed={"raw_text":full}` 兜底过宽。dry-run 直接 `python3 json.loads(raw_text)` 三卷全合法、各 150 章。SSE handler 拼 chunk 时附加了某种 trailing/control 字符导致 parse 失败，但兜底吞掉报错让链路静默断。

### 2. V3B 续跑（绕过 PR-VOL2-PARSE，直接从 raw_text 建章）

- **R1 建章** (backend 容器内 SQLAlchemy 直写) — vol 2/3/4 各 INSERT 150 章 = **450 章** ✅
- **R2 ETL** `run_full_etl(db, PID)` — `world_rules=29` ✅、`characters/foreshadows=0`（这一步只读章纲，没正文）
- **R3 章细化大纲** vol 2 ch 1-10 顺序 — **10/10 OK**，各 10-31 KB SSE 流，平均 ≈ 20 s/章
- **R4 章正文** vol 2 ch 1-10 并发 ×4 — **10/10 OK**，平均 ≈ 8 919 字符 / 章（target 3 000 字 ≈ 6 000-9 000 chars，符合），平均 290 s / 章

### 3. 14 项探针实证（V3B 跑完后取）

| # | 维度 | 数值 | 判定 |
|---|---|---|---|
| 1 | chapters total/with_outline/with_text | **450 / 450 / 10** | ✅ |
| 2 | characters | **47** | ✅ chapter 生成时触发 entity extraction |
| 3 | locations | **40** | ✅ |
| 4 | world_rules | **29** | ✅ R2 ETL 生效 |
| 5 | relationships | **51** | ✅ |
| 6 | character_states | **51** | ✅ |
| 7 | foreshadows | **0** | ❌ **伏笔提取链路完全断**（核心业务！） |
| 8 | organizations | **0** | ❌ 组织提取没触发（汇丰、军统、76 号都没建） |
| 9 | items / item_events | **0 / 0** | ❌ 道具提取没触发（核心道具「外滩怀表」漏掉） |
| 10 | faction_events / faction_oppositions | **0 / 0** | ❌ 派系事件链路缺失 |
| 11 | chapter_versions | **0** | ❌ SSE 直写 `chapters.content_text`，没建版本节点 → git-native 主张落空 |
| 12 | chapter_evaluations | **0** | ⚠️ `auto_revise=False` 业务设计，预期 |
| 13 | Neo4j (Character 47 / Location 40 / WorldRule 29 / ExtractionMarker 10) | ✅ | 同步写回 |
| 14 | 三线关键字（主线 / 感情 / 伏笔）grep on vol2 ch1-10 content | **全 0** | ❌ 三线注入到 prompt 但生成的正文没有显式标识，无法量化验证 |

附带 schema 偏差（影响 plan 文档准确性）：
- `characters` 表只有 `name + profile_json`（之前文档假设的 `role` 列不存在）
- `chapter_time_anchors.chapter_idx` 而非 `chapter_id`（外键设计偏弱，重命名章节不会更新锚点）

### 4. 修订 Plan — 17 PR 按优先级排序

#### Phase I 「修正不生效」（**最高优先级**，正在跑的项目能直接吃到）

| PR | 内容 | 触点 | 立判 |
|---|---|---|---|
| **PR-VOL2-PARSE** ⛔ | `volumes.py:217` 改为 `json.loads` 失败时**显式 raise + log 完整 chunk**；二级 fallback 用 `_extract_largest_json_object()` 分块扫描 | `backend/app/api/volumes.py` | 修后 V2 链路自然恢复，450 章自动建 |
| **PR-FACTS-FORE** ⛔ | `outline_to_facts.run_full_etl` 增加 `extract_foreshadows_from_volume_outline()`；prompt 把每章 outline_json 的 `foreshadow_state` 字段抽出 | `backend/app/services/outline_to_facts.py` + prompt | 跑完应有 ≥30 伏笔行 |
| **PR-FACTS-ORG** ⛔ | ETL 同步抽 `organizations`（军统/中统/76号/汇丰/同盟会等）+ `character_organizations` 关联 | 同上 | ≥8 组织 |
| **PR-FACTS-ITEM** ⛔ | ETL 同步抽 `items` 与 `item_events`（怀表/灰灯/封签等）+ Neo4j `Item` 节点 | 同上 + Neo4j writeback | ≥10 道具 |
| **PR-VER1** ⛔ | chapter SSE 流写完后**强制建** `chapter_versions` 行 `source="ai_generation" is_active=1`，启用 git-native 版本树 | `backend/app/api/generate.py` chapter post-save | ver count = chapter count |
| PR-EVAL1 | chapter SSE 完毕后异步触发 `ChapterEvaluator`（不阻塞返回），写 `chapter_evaluations`（即使 `auto_revise=False`） | 同上 + service | eval count ≥ chapter count |
| PR-SSE-FIX | volume outline SSE 端 600 s+ 心跳 keepalive；客户端断开时不丢已生成内容（vol 1/5 fail 的成因） | `volumes.py` + uvicorn | vol 1/5 重跑 OK |
| PR-OL10-fix | 修复全书大纲「下输出 5 卷而非 3-5」（再说一次：约束词被忽略，需提到 system prompt 顶部 + 后置校验回退） | `backend/app/services/book_outline.py` | volume_plan ≤ 5 卷 |
| PR-WIRE1 | 三线（主线/感情线/世界观）注入 prompt 后，**在生成的章节末尾追加 `<!-- strand: main=… love=… world=… -->` 注释**，让正文级量化可验证 | chapter generator template | grep 命中率 ≥ 90 % |

#### Phase II Mem-Forever 借鉴（4 PR，msg 6 决策）

| PR | 内容 | 来源 |
|---|---|---|
| PR-MEM1 | 四属性记忆元（who/when/where/what + decay/contradiction/mutation）→ 给 `character_states` 加 `decay_score / mutated_at / contradicts_state_id` 列 | Mem-Forever |
| PR-MEM3 | git-native 时间线：每个 character_state 变更走 `chapter_versions`-style 版本树 | Mem-Forever |
| PR-MEM2 | soul-memory：把 `style_profile` 切到「记忆体」结构，章正文写完后增量更新 | Mem-Forever |
| PR-MEM4 | 审稿/偏好录入 → 强依赖 PR-UI1/2/3 落地后 | Mem-Forever |

#### Phase III UI（3 PR，msg 7 决策，PR-MEM4 阻塞项）

| PR | 内容 |
|---|---|
| PR-UI1 | author_profile 录入页（口味、禁词、偏好节奏）|
| PR-UI2 | 审稿面板（章节级 issue 列表 + 修改建议）|
| PR-UI3 | 三线进度可视化（每卷一条三色线 = 主线 / 感情 / 世界观，节点 = 章）|

#### Phase IV Neo4j 状态机增强（2 PR，去重之前 NEO1-4 中已实现的 Location）

| PR | 内容 |
|---|---|
| PR-NEO5 | Time/Era/TimeEvent + `OCCURS_AT` 关系；用 `chapter_time_anchors` 实证派生 |
| PR-NEO6 | FactionEvent（结盟/破盟/开战/休战）从 `faction_events` 表派生写回 |

#### Phase V 三线增强（3 PR，msg 6 决策）

| PR | 内容 |
|---|---|
| PR-STRAND1 | 章生成 prompt 显式列出当前章应推进的三线节点（来自卷大纲 chapter_summary 的 main_progress / side_progress） |
| PR-STRAND2 | 章生成完后 strand_tracker 量化每条线推进 + 写 `strand_progress` 表 |
| PR-STRAND3 | UI 三线 dashboard（接 PR-UI3） |

#### Phase VI 朱雀 V1 CH2 第二轮 reduction（沿用前序 plan）

### 5. 跑通 Phase I 后的复跑验收脚本

复用 `/tmp/build_and_etl.py` + `/tmp/orchestrate_v3b.py`，跑完后预期：
- chapters 750 / 750 / 750（5 卷 × 150 章全建 + 全有 outline + 全有 text）
- foreshadows ≥ 200、organizations ≥ 8、items ≥ 30
- chapter_versions = 750、chapter_evaluations = 750
- 三线注释 grep 命中率 ≥ 90 %

### 6. 当前未决 / Follow-up

- vol 1（外滩怀表）/ vol 5（红玉无声）卷大纲未落库 → PR-SSE-FIX 修后重跑
- characters.role 列实际不存在（在 `profile_json.role`）→ 文档校正条目
- chapter_time_anchors.chapter_idx 而非 chapter_id → 设计偏弱，章节重排会丢锚

### 7. 临时 artefact（不入 git）

- `/tmp/orchestrate_v2.py` `/tmp/orchestrate_v3b.py` `/tmp/build_and_etl.py` `/tmp/probes_v3b.sh`
- `/tmp/orchestrate_v2_status.json` `/tmp/orchestrate_v3b_status.json` `/tmp/probes_v3b.out`
- `/tmp/sse_v3b_outline_v2c{1..10}.log` `/tmp/sse_v3b_text_v2c{1..10}.log`

---

## B2 — Phase II 修复（2026-05-03，feat/phase2-fix）

### 触发问题（用户实测，user msg 12 + 13）
1. 三线平衡显示 ⚠Quest/Fire/Constellation 已 150 章未推进。
2. 伏笔追踪都是 50%。
3. 设定集 → 人物 profile 全为空。
4. 设定集 → 世界规则全无。
5. 右侧「查看全书/分卷/章节大纲」点击无效。
6. 第一卷为空，从第二卷开始生成内容。
7. TOKEN 用量始终为 0。
8. 全书大纲文本中泄露 `<volume-plan>...</volume-plan>` LLM 控制标签。

### 修复（6 个 commit on feat/phase2-fix，已 push）

| # | Commit | PR | 文件 | 解决症状 |
|---|---|---|---|---|
| 1 | `5ab7782` | PR-OL15 + PARSE-VOL | outline_generator.py / generate.py / OutlineTree.tsx / MobileWorkspace.tsx | #6 #8 + 解锁下游结构化数据流 |
| 2 | `e49a05f` | PR-FACTS-CHAR-PROFILE | outline_to_facts.py | #3 |
| 3 | `7dbad7c` | PR-USAGE-SYNC | llm_call_logger.py | #7 |
| 4 | `64743cc` | PR-WORLDRULES-FE | SettingsPanel.tsx | #4 |
| 5 | `ef06e24` | PR-OUTLINE-BUTTONS | chapters.py | #5（章节大纲按钮）|
| 6 | `260cbb8` | PR-STRAND-OUTLINE | strand_tracker.py | #1 |

#9 伏笔 50%（user msg 12 第二项）= 「unresolved/total = 当前所有有 setup 但暂未 resolve 的伏笔比例」 → 50% 是符合预期的 mid-stream 比率，不修。

### 关键根因发现（第二轮深探）

- **B2 #1 + #3 共享根因 — 写盘缺陷**：`generate.py:943` 的 `_content_json = {"raw_text": full_text}` 仅书级提取 `volume_plan`，卷/章级别**从未把 LLM JSON 解析出来**，导致 vol2 outline content_json 实测 `keys=['raw_text']`、`new_characters count=0`，下游所有 ETL 即使流程正确也拿不到结构化数据。**PR-OL15 + PARSE-VOL 同时解决了 B2 #6 + #8 + 解锁 #3 + #7 数据链路**。
- **B2 #5 章节大纲按钮**：根因不是 onClick 失效，是 `lightweight=true` 后端省略 `outline_json` 字段，前端条件 `Boolean(chapter.outline_json)` 永远 false。
- **B2 #4 世界规则**：根因不是后端缺路由（路由 `/api/projects/{pid}/world-rules` 存在），是前端 `RuleResp { rules?: ... }` 字段名不匹配（后端返回 `world_rules`）。

### 实测验证（V2 PID）

```
=== before B2 ===
characters=47        with_profile_json=0  ← #3
world_rules=83       (后端有数据)        ← #4 字段名错
foreshadows=450
organizations=36
items=49
outlines vol2 keys=['raw_text']         ← B2 写盘缺陷证据
outlines tag occurrences <volume-plan>=1

=== after B2 (代码 patch + SQL 数据回填) ===
characters=66        with_profile_json=14 ← +14 非空 ✅
world_rules=111      (前端可正确渲染 ✅)
foreshadows=463      ← +13
outlines vol2 keys 补足 chapter_summaries / new_characters / world_rules / volume_idx ✅
outlines tag occurrences <volume-plan>=0  ✅
```

### 数据补救

- **SQL/Python 一次性脚本**（`/tmp/p2_clean_outlines.py`）：14 行 outline 中 1 行 strip volume-plan tag、13 行从 raw_text 反向解析回填 `chapter_summaries / new_characters / world_rules / volume_idx`。
- **ETL 重跑**：etl_characters / world_rules / foreshadows / organizations / items 依次跑通，profile_json 14 行非空。
- **vol1 + vol5 SSE 重跑**：后台 nohup 进行中（pid 4065651 / 4065756，约 4-6 min/卷）。完成后 `chapters` 表 v1/v5 各 150 章。

### Follow-up

- 前端代码改动需要重启 Next.js dev server（无 docker 容器，`cd frontend && npm run dev`）或重新 build。
- 后端已重启，5 个后端 patch 全部生效。
- 待 vol1/vol5 SSE 跑完，再次跑 ETL，characters 有望追加新角色，foreshadows 也会增长。

## 2026-05-03 22:25 — 502 修复 + frontend 镜像 rebuild

### 真根因
- 用户访问 8080 → ai-write-nginx 反代
- ai-write-nginx 配置 set $frontend_upstream http://frontend:3000 + resolver 127.0.0.11（docker 内部 DNS）
- nginx error log: frontend could not be resolved (2: Server failure)
- docker compose ps: frontend 服务 missing（其他 9 容器 Up）
- 宿主机有个 next-server 进程跑在 *:3000 但不在 docker network 里 → ai-write-nginx 不可达 → 502

### 修复
1. docker compose up -d frontend → 容器起来，烟测 8080 返回 200（root/login/api/health）
2. 杀宿主机 stale next-server PID 4088553（释放 *:3000，避免后续混淆）
3. 发现 ai-write-frontend:latest 镜像是 2026-05-02T22:00Z build，早于 PR-WORLDRULES-FE / PR-OL14 / PR-OL15（2026-05-03 02:54~09:26）
4. docker compose build frontend → 新镜像 2026-05-03T14:24Z，自动 recreate 容器
5. 烟测 8080 全部 200，nginx access log 干净

### 认知更正
- 127.0.0.1:3001 是 grafana docker-proxy（不是 next-server）
- next-server 之前一直在宿主机 *:3000，但用户看到的是 8080→nginx→docker frontend:3000；所以前端 commit 是否生效取决于 docker 镜像 build 时间，不是宿主机进程
- user msg 12/15 报的 5 症状之所以"修了还在"，部分原因是新前端代码从未真正进入 ai-write-frontend 镜像

---

## 2026-05-06 07:40 仿写验证实测进展与阻塞

- **Stage B ✅**：项目已建 `df6f523e-f903-4644-bcce-636f5ed89c68`，settings_json.style_reference 已绑赤心 profile `b76da43a-...` + reference_book `0a543b1d-...`。
  - 隐患：POST `/api/projects` 入参 `target_word_count` 被忽略，Project 落默认 3000000。已 PUT 修正为 200000。`backend/app/api/projects.py:58 create_project` 未传 `body.target_word_count` 给 `Project(...)`。
- **Stage C ⚠️ 阻塞**：book outline `0868f734-6e9c-4210-bbce-e09ac8c5adaa` 已落库，但 `content_json = {"raw_text": "..."}`。
  - staged_stream 三阶段（A骨架 / B角色 / C世界观）full_text 未被 `OutlineGenerator._parse_json` 解析为 `main_plot / volume_plan / characters / world_setting / chapter_naming_style`。
  - 下游 volume / chapter outline 依赖 `book_outline.content_json["main_plot"]`（`generate.py:166`）与 `volume_plan` ⇒ 取不到，无法推进。
  - 根因：`_persist_outline_now`（`generate.py:1060-1090`） 只在 `level=="book"` 调 `_extract_volume_plan`；结构化 `_parse_json` 只走 `level in ("volume","chapter")` 分支。
- **Stage D / E**：未开始，依赖 Stage C 产出。

### 新 PR 候选：PR-OUTLINE-STAGED-PERSIST-STRUCT

触点 `backend/app/api/generate.py:1060-1090`，修复方向：

- A（轻量）：把 `level in ("volume","chapter")` 分支的 `_parsed_struct = _OG()._parse_json(full_text_clean)` 逻辑同时应用于 `level=="book"`，setdefault 进 `_content_json`。
- B（严谨）：staged_stream 三阶段各自 `stage_end` 时将其 full_text 解析为 JSON，最后合并（A 出 main_plot+volume_plan、B 出 characters、C 出 world_setting）。推荐。
- 顺带修 `backend/app/api/projects.py:58 create_project`：加 `target_word_count=body.target_word_count`，POST 与 GET 一致。
- 验收：重走 Stage B 创项 → Stage C book outline → `SELECT content_json FROM outlines WHERE id=...` 含 `main_plot/volume_plan/characters/world_setting`；volume outline 走通。

## 2026-05-06 22:02 PR-OUTLINE-STAGED-PERSIST-STRUCT 落地 + 验证

### 修复
- `backend/app/services/outline_generator.py`：`_generate_book_outline_staged_stream` 在 yield done 前用新增的 `_split_book_sections(skeleton, characters, world)` 把三段 buffer 按 `^[一-九]、` 切片，再用 `_build_book_structured(buckets)` 拼出 `{main_plot, characters, world_setting, chapter_naming_style, sections}`，随 `volume_plan` 一并塞进 done event。
- `backend/app/api/generate.py`：staged_stream 处理新增 `_staged_book_done_payload` 捕获；`_persist_outline_now` 的 `level=="book"` 分支优先用 payload 的 volume_plan，再 fallback `_vp_for_book`，并把 main_plot / characters / world_setting / chapter_naming_style / sections 各 setdefault 进 `_content_json`。失败用 try/except 落 PR-OUTLINE-STAGED-PERSIST-STRUCT promote failed log。
- `backend/app/api/projects.py:create_project` 顺带补 `target_word_count=body.target_word_count` + `genre_profile_code=body.genre_profile_code`，POST 创项即可一次到位。
- 提交：`391e053 PR-OUTLINE-STAGED-PERSIST-STRUCT: persist book outline structured fields`，已 push origin main + feat/phase2-fix。

### 验收（赤心仿写项目 `df6f523e-f903-4644-bcce-636f5ed89c68`）
- 删旧 outline `0868f734-...`，重跑 staged_stream（约 11 min，三阶段流式 OK）。
- 新 outline `15a4770c-9230-49a0-a493-700644b32862`，`outlines.content_json` 长度 136923 chars。
- top-level keys = `chapter_naming_style, characters, main_plot, raw_text, sections, volume_plan, world_setting` 全齐。
  - main_plot 95 / characters 3765 / world_setting 727 / chapter_naming_style 1029 / raw_text 7653。
  - sections dict 9 个 key（一-九）齐全。
  - volume_plan = 1 卷，title=`照骨登山`，est_chapters=50，含 idx/theme/core_conflict。
- 模型选了"50 章 1 卷"结构（不是原计划 5 卷 30 章），下游 volume outline 跑 1 次即可。

### Stage C remainder
- volume outline x1（vol1，est 50 章）SSE 已起后台。
- chapter outline x前3章 待 volume done 后启动。

## 2026-05-06 14:45 Stage C remainder + Stage D/E 完成（赤心仿写验证闭环）

### Stage C remainder
- vol1 outline `803b025e-5347-4eb3-bb18-780169f6a732`（level=volume，parent=`15a4770c-...`，86298 chars，12 keys，50 chapter_summaries，已 confirm）。
- chapter outline x3（均 SSE staged_stream）：
  - ch1 `b0833dd1-f5ab-4315-be57-f3b346f5bcaa` 17007 chars
  - ch2 `92d98208-b015-4201-9f91-6c72b3157040` 19204 chars
  - ch3 `42837860-8983-4c83-8899-1cb1f5cb443d` 16507 chars
- volume row 物化：走 docker exec `/tmp/materialize_vol1.py` 推夑50 chapters（vol1 row `ee36b649-...`，target=12000/章）。已知限制：PR-OL2 自动物化路径不存在，当前依赖手动脚本；后续候选 PR 补上 vol staged_stream done 后的 materialize（参考 `volumes.regenerate_volume:143-329`）。

### Stage D 三章正文（scene_mode + auto_revise）
- endpoint：`POST /api/generate/chapter`，use_scene_mode=true、target_words=12000、auto_revise=true、max_revise_rounds=2、revise_threshold=7.0。
- 均串行（避免 LLM rate limit + DB 争用）。单章端到端 ~8 min，scene_planner 阶段 SSE 静默 ~3 min 后转为 token 流式。
- 产出：
  - ch1 row `a01873a2-...` 「义庄夜收无名尸」 14940 字，score=8.28，issues=16，revise skipped。
  - ch2 row `9294003b-...` 「按印者欠命」 16690 字，score=8.28，issues=14，revise skipped。
  - ch3 row `75c42b05-...` 「夜更三十三响」 13431 字，score=8.36，issues=16，revise skipped。
- 人工抽检三章 head/tail 与赤心 profile 风格全部匹配：物候五感、短句顿挫、制度神化（账=命/印=债/丁字号=点名）、底层书生气、章末钩子（天亮了 / 钟声从灰里过来 / 先认账）。

### Stage E
- 报告落地：`docs/CHIXIN_VALIDATION_REPORT_2026-05-05.md`。8 节 + 关键 ID 速查 + commit chain。
- **综合结论**：赤心仿写链路闭环验证通过。默认参数（threshold=7.0）下不需 revise 即可达到 8.28+ 评分，正文 12-17K 字，风格忠实。

### 临时资产
- `/tmp/ch{1,2,3}_full.txt`（三章正文原文，43K/49K/39K）、`/tmp/ch{1,2,3}_gen_sse.log`（SSE 原始事件，351K/388K/314K）、`/tmp/materialize_vol1.py`（同名复制到 backend 容器 :/tmp/，已跑）。

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

## 2026-05-07 vol1 ch4-10 完整补齐

- ch4 手动跑完：14029 字 / 8.36 / revise_skipped。
- ch5-ch10 两段 driver 串行跑完（PR-OL2 验证路径）：
  - `/tmp/ch5_to_10_driver.sh` PID 827545：ch5 / ch6 / ch7 / ch8 顺利 completed；ch9 被 NVIDIA SSE `INTERNAL_ERROR` (stream RST) 中断，driver 退出。
  - `/tmp/ch9_10_resume.sh` PID 3119019：ch9 / ch10 重试完成，含 3 次重试 + sleep 30-45s。
- ch5-ch10 全部使用 PR-OL2 vol-level 自动物化的 chapter row（无需手动 INSERT），路径 idempotent。
- ch8 / ch9 章名被 chapter outline 阶段重写为第二人称（违反 rename 约束），已 SQL 强制还原：
  - ch8 `骨灯认你，也要你命` → `骨灯认主夜不止`
  - ch9 `你把他写得太干净` → `簿改丁七为流民`
- ch9/ch10 重试路径未走 auto_revise evaluation，字数从 13-16k 跌到 ~10k；记入 backlog，不阻塞收尾。
- vol1 ch1-10 合计 139,602 字，10/10 completed。

## 2026-05-07 · PR-TITLE-Q1 + PR-CHGEN-ALIAS

应对用户迫切说明「不要以后人工发现又去 SQL 改名，不是禁第二人称，是要符合逻辑和语言习惯」：

- 新增 `backend/app/services/title_quality_checker.py`：规则层（`object_abstract_verb`、`2p_meta_address`、占位/全角冒号/现代词/抽象空词/字数边界）+ LLM 重写层（batch rewrite + 二次校验）。
- `outline_generator.py` `VOLUME_CHAPTERS_SYSTEM` 重写 + 后置 `check_and_rewrite_in_place` hook；vol-outline 生成后、落库前全量质量门。
- `chapter_outline_expander.py` SYSTEM_PROMPT 加 title 冻结声明 + `_validate_and_normalize` 中 title 优先级翻转（chapter.title > stub.title > parsed.title）：chapter-outline LLM 不再能覆盖 vol-outline 定下的 title。
- `api/generate.py` `GenerateChapterRequest` 加 `@model_validator(mode="before")`，容错 `scene_mode` / `scene_mode_on` 两个古别名 → `use_scene_mode`；修复 ch9/ch10 retry 为什么 SSE 没 `event: scored` 的根因（driver payload 字段名不匹配 → 静默走单次 ChapterGenerator）。

单元自测 8/10 PASS；backend 已重启、Application startup complete。
详见 `docs/PR_TITLE_Q1_2026-05-07.md`。

---

## 2026-05-08T16:00Z→ codex auth 恢复，Stage D 重启

### 里程碑
- T+0: 上游 codex provider auth 修复。
- T+2 min: ch11 outline expand 验证通过（30 min 前还 503）。HTTP 200 / 80.5s / key_events=4。
- T+10 min: ch11 内容生成 SSE 完整闭环。**word_count=12225, score=8.18, issues=15, revise_skipped, completed**。
- T+10 min: 启动 Stage D batch driver `scripts/stage_d_batch.sh 12-20`。

### PR-CHGEN-ALIAS 验证证据
顶不再需要推论：ch11 SSE trace 明确包含 `event: scored round=1 overall=8.18 issues=15` 与 `event: revise_skipped reason=score_above_threshold`；ch9/ch10 retries 时的「empty scored」现象未重现。alias 路径走了 SceneOrchestrator+Evaluator，不是 single-shot fallback。

### Stage D 当前进度
- vol1 ch1-10: completed (139,602 字，均分 8.28-8.52)
- vol1 ch11: completed (12,225 字，8.18)
- vol1 ch12-20: batch 运行中 (pid 2832166)
- vol1 ch21-30: chapter rows 已物化 (draft)，等 ch12-20 跳完决定是否续跳


### 2026-05-09T12:25Z (北京 5/9 20:25) — Batch B 部分成功 + codex 二度阻塞

**Batch B (pid 1595272, start 11:46:03Z, end 12:24:43Z)**
- max-time 从 1500 提升至 2400s/章 (针对 batch A ch19/20 1500s curl timeout)
- ch19 重跑 ✅: 11102 字, score 8.38, revise_skipped (gen 865s)
- ch20 重跑 ✅: 13694 字, score 8.36, revise_skipped (gen 516s) — 覆盖了 batch A 10108字+无 score 的截断版
- ch21 ✗: expand 116s OK, gen 652s rc=0 但 word_count=0 (SSE 52KB 半成品, scene_writer 中段 401)
- ch22-30 ✗: expand HTTP=500 全部 1-2s 内返回 (12:24:31-43 12 秒集中失败)

**根因**: codex 上游 auth.json 于 12:24:29Z 返回 401 (`Your authentication token has been invalidated`)。Backend stream tier-fallback (ep dfd26325 + ac6eb9cd) 两档全部 401, RuntimeError 中断 scene_writer。后续 expand 调用 codex 返 503 `auth_not_found providers=codex`。同 5/8 故障字面值完全一致。

**vol1 现状**:
- ch1-20 全部 completed = 266,931 字
- ch11-20 均过 7.0 阈值 (score 7.64 - 8.54), 均 revise_skipped
- ch21-30 等 codex 恢复

**调整落库**:
- scripts/stage_d_batch.sh max-time 1500 → 2400 (保留)
- Batch A 旧日志备份为 /tmp/stage_d_run_b1.log
- Batch B 日志 /tmp/stage_d_run.log 保留供调法


## 2026-05-09 (PR-CHIXIN-ANTI-AI)

- **代码**：`features_to_rules` 加 LLM anti_ai_rules 合并路径；LLM_STYLE_PROMPT 16 维度（+第 16 项 anti_ai_rules，要求书风专属陷阱）；新增 `POST /api/styles/{id}/regenerate-anti-ai` 端点（仅回填 anti_ai_rules）；3 个新单元测试 PASS。
- **数据**：5 个 style_profile 全部修为 `bind_level=book` + 有效 `bind_target_id`（赤心 → 0a543b1d；天之炽 → 67fe33f9；天之炽②女武神 → c33c2f19）。
- **回填**：赤心巡天 anti_ai_rules 0 → 13 条（端点调用耗时 528 s，LLM 单次完成）。
- **Stage D**：范围澄清为 ch1-20，ch21-30 不跑（kill 并清理 draft）。
- **docs**：CHIXIN_VALIDATION_REPORT §8.7 / HANDOFF_TODO PR-CHIXIN-ANTI-AI 段同步落档。


## 2026-05-09 (PR-CHIXIN-REGEN-V2 根因+根治脚本落档)

- 在跑 batch ch1-20 中查明 ch2 split-brain 三层根因（LLM 真实推理时间 / curl 2400s 不够 / 章间并发污染）
- 新增 `scripts/stage_d_batch_v2.sh` (5400s + 90s cooldown + DB-truth verdict)
- 新增 `scripts/stage_d_repair.sh` (扫描差章自动重跑)
- 备份 v1.2 ch1-20：/tmp/chixin_vol1_ch1-20_backup_v1.2_*.sql + .json
- 不打断当前 batch (PID 1843380)，跑完后用 repair 头扫补差章
- backend SSE close 改进进 backlog (不动当前 batch)


## 2026-05-10 09:09-09:18 插入故障：流程测试2 ch21/22 误清空 + 恢复

- 09:09 user 报告：点正文准备复制时内容被误删
- 根因：后端 log 确认 5/10 01:09:10 + 01:10:01 两次 `PUT /api/projects/project_id/chapters/chapter_id`、king 账号、`content_text=""` 覆盖
- 备份：/tmp/recover_lct2/versions_backup.json (41 KB) + chapters_before_recovery.json (18 KB)
- 恢复：从 chapter_versions 里 5/4 19:23 初稿 UPDATE chapters。ch21 《无名尸失踪》 6447字、ch22 《馆里有人嗂狗》 7505字全部回魂
- backlog 列入 PR-CHAPTER-PROTECT-V1 三项（后端 guard / 前端定位 / versions 自动 snapshot）——用户 09:18 明确“后续再做，现在回赤心巡天”

---

## 2026-05-12 — PR-GEN-REVISE-DEDUP 完工 / Vol1 20/20 PASS

**背景**：Stage D 初跨后 6 章低于 7.0，issues_json 共性 = scene 重复推进 / 场景回拨。

**代码**：同名分支 `feat/pr-gen-revise-dedup` 已合 main。patch 点：scene_orchestrator.plan_scenes 注入「场景互斥硬约束」 / write_scene_stream prior_block 改为「已发生禁重写」。DB prompt_assets `scene_planner` 647→911 / `scene_writer` 443→632。

**验证结果**：
| ch | baseline | new |
|---|---|---|
| 2 | 5.64 | **7.18** |
| 8 | 6.90 | **7.86** |
| 10 | 6.54 | **8.42** |
| 12 | 4.80 | **7.62** |
| 15 | 5.82 | 8.28 (旧评) |
| 16 | 7.02 | **7.86** |

**Vol1 终态**：20/20 ≥ 7.0，均分 7.97，总 223,167 字。Stage E 验证报告 `docs/CHIXIN_VALIDATION_REPORT_2026-05-05.md`。

**Backlog 上提**：PR-CHAPTER-PROTECT-V1 (PUT guard / 前端定位 / 自动 snapshot) / PR-GEN-SSE-FINALIZE (ch10/12/15 silent-skip 同根)。

## 2026-05-26 16:46:43 +0000 — 通用 v4.5 生成前可执行证据-动作账

- 背景：第五章 v4.4 `world_logic_contract_v4_4_preflight_continuity_evidence_budget` 第一稿未达标（overall=7.12，issues=13，repeat=13），问题集中在信息提前命名、权限机制例外含混、强弱资源差无代价、动作链物理支撑不足、章末奖励路径不足。
- 通用化原则：本次修复不得写入第五章专名或当前项目特化规则；目标是跨章节、跨项目复用，减少同类 `information_rule_violation / mechanism_rule_violation / power_resource_violation / result_strength_violation / space_rule_violation / time_rule_violation / expression_contract_violation`。
- 代码版本：`NARRATIVE_CONTRACT_VERSION = "world_logic_contract_v4_5_preflight_executable_ledger"`。
- 核心 prompt：`direct_generation_first_v4.5` / `evidence_name_ledger` / `authority_procedure_ledger` / `interference_action_ledger` / `reward_provenance_ledger` / `negative_permission_before_source` / `payoff_after_source_action_cost` / `downgrade_if_ledger_missing` / `runtime_prompt_snapshot`。
- 生效机制：`ChapterGenerator._with_preflight_blueprint` 注入通用 v4.5 system prompt，并记录 `preflight_contract_snapshot contract_version=... chapter_idx=... prompt_hash=... markers_missing=...`。该日志只用于观测 prompt 是否进入运行时，不阻断生成、不触发后置修订、不制造 `needs_repair`。
- 生成策略：继续保留 `force_direct_chapter=true` 与 `skip_scene_planner=true` 可用，避免 scene_planner JSON list 造成 0 字卡死；质量收敛依靠生成前内化账本，而非后置 hard gate / auto_revise 主流程。
- 验证要求：重启 backend/celery 后 smoke 必须确认 backend/celery import 的版本为 v4.5，prompt marker 全部存在，celery ping 正常，metrics 200；随后以第一稿验证观察 overall/issues/repeat 与 issue tag 分布是否下降。

## 2026-05-27 · v4.6 hierarchical_outline_contract 通用层级大纲来源合同

- 背景：v4.5 `world_logic_contract_v4_5_preflight_executable_ledger` 已确认运行时注入有效，但 Chapter 5 first draft 仍为 overall=7.48、issues=14、repeat=14，未达到 overall>=8.2/issues<=12/repeat<=12。主要失败不是 prompt 未加载，而是章节大纲未成为正文生成的“唯一剧情源/执行合同”。
- 用户约束：章节大纲应是本章全部剧情；正文只做扩写、润色、场景化、动作化、因果支撑，不应再让 LLM 新增剧情。并且分卷大纲受全书大纲约束、章节大纲受分卷大纲约束。
- 新版本：`world_logic_contract_v4_6_hierarchical_outline_contract`。
- 通用原则：全书大纲是剧情宪法；分卷只能细化全书；章节只能细化分卷；正文只能扩写章节。下层不得新增上层未授权的剧情功能、强线索、强结论、逃生捷径、章末硬奖励或提前兑现。
- 新增生成前 markers：`direct_generation_first_v4.6`、`hierarchical_outline_contract`、`outline_lineage_contract`、`book_to_volume_contract`、`volume_to_chapter_contract`、`chapter_outline_source_of_truth`、`prose_expand_only_contract`、`result_ceiling_from_outline`、`outline_drift_precheck`、`no_new_plot_in_prose`、`runtime_prompt_snapshot`。
- force_direct 路径同步强化：跳过 scene_planner 时也必须以“全书大纲→分卷大纲→章节大纲”的层级来源链为唯一剧情来源，章节正文只允许扩写润色。
- 仍保持：不引入后置 hard gate / needs_repair / auto_revise 主流程；评分 issue 仅用于诊断和下一轮生成前内化。
- 已验证：本地 `python3 -m py_compile` 通过，后续需重启 backend/celery、smoke 并触发 v4.6 first-draft 验证。


## 2026-05-27 01:30:54 +0000 v4.6 async task routing and chunk normalization
- Fixed generic `/api/generate/async` task-type inference: if `chapter_id` is present and `task_type` is omitted, create a `chapter` task instead of defaulting to `outline_book`.
- Added generic async chunk normalization in `knowledge_tasks.py` so streamed dict chunks are converted to text before append/join, preventing `sequence item 0: expected str instance, dict found`.
- Reusable across chapters/projects and outline/chapter async tasks; not a Chapter 5 special case.


## 2026-05-27 v4.7 outline execution units
- Updated contract to `world_logic_contract_v4_7_outline_execution_units`.
- Replaced v4.6 hierarchical-outline-only scaffold with generic pre-generation `outline_execution_units` and `chapter_outline_unit_ledger`.
- v4.7 keeps the user-required direction: first draft should internalize constraints before generation; no hard gate / needs_repair / auto_revise main flow; no scene_planner dependency.
- Each chapter outline node must become execution units with cause/effect bridge, information source, resource pressure, character response, foreshadow handling, result state delta, result ceiling, and unresolved tail.
- Strengthened force-direct fallback instruction so chapter generation remains usable across chapters/projects when scene planner JSON is unstable.


## 2026-05-27 v4.8 anchor audit before prose
- Updated generic narrative contract to `world_logic_contract_v4_8_anchor_audit_before_prose`.
- Added direct-generation-first `anchor_audit_before_prose` requirements to preflight prompt: source anchors, transfer paths, observation windows, adversary incentives, costs/interference, downgrade rules, and payoff ceilings before reveal.
- Updated runtime prompt markers and force-direct fallback instructions.
- This remains generation-before internalization, not a hard gate / needs_repair / post-output blocking workflow.

## 2026-05-27 v4.10 generation timeout propagation

- Added per-call timeout propagation for long single-shot chapter generation.
- `ChapterGenerator.generate()` now accepts `request_timeout`, `retry_attempts`, and `stream` routing kwargs and passes them through `run_text_prompt()` to the model router.
- Force-direct and single-shot fallback chapter paths now derive an LLM request timeout from `fallback_timeout_seconds` via `SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS` and use `SINGLE_SHOT_LLM_RETRY_ATTEMPTS` (default 1) so tier fallback can happen within the outer task budget.
- This is a generic cross-chapter/cross-project fix for 0-character direct-generation failures caused by the old global 75-second OpenAI-compatible request timeout; it does not add post-output quality gates or auto-revision as the main flow.

## 2026-05-27 v4.10 single-shot generation non-stream fallback
- Context: v4.10 timeout propagation reached longer endpoint attempts, but the retry still ended with `APIError: stream error: stream ID 7; INTERNAL_ERROR; received from peer` and persisted a 0-character draft for task `9f13c288-5a3d-4c63-bd7f-d2294d3ddd91`.
- Change: direct/fallback chapter prose now passes a generic `stream` kwarg from `SINGLE_SHOT_LLM_STREAM`, defaulting to non-streaming (`0`) for single-shot generation while leaving normal chat/global `OPENAI_CHAT_STREAM` behavior unchanged.
- Scope: reusable for all chapters/projects using force-direct or fallback chapter generation; not a chapter-specific repair, not a post-output quality gate, and not an auto-revision workflow.
- Expected effect: avoid losing the whole draft when an upstream streaming route fails late; the request still uses bounded request timeout and one provider attempt per endpoint.


## 2026-05-27 v4.10 force-direct durable persist recovery
- Context: SQL3 validation task `a3bc30ba-015e-4357-937b-154e6e32a8bb` proved non-stream timeout budgeting can return a full draft (`char_count=7457`), but a stale asyncpg connection during post-LLM persistence caused a failed task even after fresh-session progress persistence.
- Change: after force-direct prose is durably persisted through a fresh session, the task no longer reuses the old long-lived ORM session before continuing. This avoids `connection is closed` / `greenlet_spawn has not been called` errors from converting valid generated prose into a failed/0-result workflow.
- Scope: generic for all chapters/projects using force-direct or single-shot fallback chapter generation; it does not introduce post-output hard gates, auto-revision, or scene_planner dependency.
- Validation requirement: restart backend/celery, run smoke, then rerun/repair validation so a generated draft reaches completion and can be scored.


## 2026-05-27 v4.10 evaluation timeout budget alignment
- Context: after SQL3 force-direct recovery, the compensation evaluation failed as infrastructure timeout: two standard endpoints each timed out around the old 55-second request budget, producing no valid score and no synthetic zero row.
- Change: the generic evaluator request timeout default is raised from 55s to 180s and the async evaluation task wrapper default from 210s to 420s. This gives long Chinese chapter evaluation enough time to return compact JSON while preserving bounded failure behavior.
- Scope: reusable across chapters/projects; still no synthetic zero scores, no post-output quality gate, and no auto-revision as the main workflow. Evaluation remains diagnostic/observability for generation-before prompt refinement.


## 2026-05-27 v4.10 evaluation extended timeout budget
- Context: after raising evaluation request timeout to 180s, the recovered v4.10 draft still timed out on both standard endpoints, taking ~361s and producing no synthetic score row.
- Change: the generic evaluator request timeout default is raised to 420s and the async evaluation wrapper to 960s, allowing one full long-form judge attempt per endpoint while keeping bounded terminal failure semantics.
- Scope: evaluation infrastructure only; no post-output hard gate, no auto-revision, no generated prose output in logs/artifacts.


## 2026-05-27 19:30:40 +0000 v4.10 evaluation compact judge budget
- Context: recovered Chapter 5 SQL3 draft has valid 7457-char content, but compensation evaluation timed out on both standard endpoints even with 420s per endpoint / 960s wrapper. No score row was created and zero-score rows remained 0.
- Change: made the generic evaluator more compact and endpoint-friendly: default judge output budget is now 768 tokens, temperature defaults to 0.0, evaluation streaming is enabled by default, chapter/outline/summary/style/foreshadow contexts are bounded by environment-configurable character caps, and prompt length metadata is logged without prose content.
- Scope: reusable evaluation infrastructure for all chapters/projects. This does not add post-output hard gates, auto-revision, synthetic 0 scores, or scene_planner dependency; evaluation remains diagnostic/observational for generation-before prompt refinement.

## 2026-05-27 v4.10 evaluation compact retry tightening
- Context: the compact evaluator retry still failed after two ~420s endpoint attempts with streaming enabled and an ~8049-character user prompt. No score row was created, and zero-score rows stayed at 0.
- Change: tighten generic evaluator defaults again: chapter text cap 4200 chars, outline cap 1600 chars, prior summary cap 900 chars, style cap 800 chars, active foreshadow cap 12 items, judge output cap 512 tokens, and evaluator streaming default back to non-streaming.
- Scope: reusable across chapters/projects; evaluation remains diagnostic only. This does not add a post-output hard gate, auto-revision, synthetic 0 score, scene_planner dependency, or chapter-specific prose handling.

## 2026-05-28 v4.11 outline execution ledger constraints
- Context: Chapter 5 v4.10 compact-tight evaluation completed without infrastructure failure (`overall=6.46`, `issues=12`). Issue count met the target, but overall missed because failures shifted from prose mechanics to plot coherence, foreshadow strength, and character action causality.
- Change: upgraded the generic direct-generation preflight prompt to v4.11. The prompt now requires `outline_beat_execution_ledger`, `foreshadow_control_ledger`, `character_state_ledger`, and `pacing_budget_ledger` before prose. These ledgers force every chapter-outline beat to carry goal/cause/action/consequence/no-new-plot budget, prevent unauthorized foreshadow payoff, require character knowledge/motive/location/resource/cost before actions, and cap low-importance beat expansion.
- Scope: cross-chapter/cross-project prompt/contract behavior only. This is generation-before constraint internalization, not a post-output hard gate, not needs_repair/auto-revise as a main flow, and not scene_planner-dependent. Force-direct fallback remains available.

## 2026-05-28 v4.12 evidence/mechanism/uncertainty ledgers
- Upgraded narrative contract to `world_logic_contract_v4_12_evidence_mechanism_uncertainty_ledgers`.
- Added generation-before ledgers for evidence permissions, mechanism boundaries, inference uncertainty, and time-window budgets.
- Preserved and strengthened Chinese prose mechanics: sentence rhythm, plain verbs, natural collocations, physical prepositions, viewpoint flow, no long quote repetition, no action slicing.
- This remains direct-generation-first diagnostic guidance, not a post-output hard gate or repair workflow.


## 2026-05-28 v4.13 spatial/occlusion/coincidence/dialogue ledgers
- Added generic pre-generation ledgers for spatial feasibility, channel occlusion, coincidence friction, and dialogue density.
- Purpose: reduce over-convenient clue placement, impossible close-quarters movement, too-clear overheard information, and dense shout/command repetition while preserving Chinese prose mechanics.
- Still generation-first: diagnostics/markers observe prompt injection only; no post-output hard gate or repair workflow.

## 2026-05-28 07:15:47 +0000 v4.13 evaluator compact-timeout follow-up

- 原因：v4.13 compact-tight 评分任务 4c268882-68ac-426b-8397-bf6170f72ed0 因评价基础设施 APITimeout 失败，未写入伪零分。
- 通用修复：进一步压缩评价输入/输出默认预算，降低跨章节/跨项目评价超时概率：正文 4200→2600、上一章摘要 900→450、风格 800→420、章节大纲 1600→900、伏笔条目 12→6、输出 tokens 512→384、单次请求 420s→360s。
- 原则：只影响评分基础设施稳定性，不改变正文生成主流程；不引入 hard gate / needs_repair / 后置阻断作为主流程。

## 2026-05-28 v4.13 evaluator ultra-compact retry follow-up
- Context: Chapter 5 v4.13 evaluation retry still failed with `EvaluationInfrastructureError` caused by APITimeout after the prior compact-timeout reduction.
- Change: tightened evaluator diagnostic defaults generically to reduce request size and expected response length: chapter text 1400 chars, previous summary/style 260 chars, outline 520 chars, active foreshadow items 4, max tokens 256, request timeout 240s.
- Scope: scoring infrastructure stability only. This does not change chapter generation, does not introduce post-output hard gates, and does not add `needs_repair` or blocking repair as the main workflow.

## 2026-05-28 v4.13 direct-generation persistence salvage
- Context: Chapter 5 v4.13 direct generation produced valid prose, but the original long-lived async DB session later hit a persistence/session error (`connection is closed` / `MissingGreenlet` class) and marked the task failed until manually recovered.
- Change: the async generation failure handler now first checks whether non-empty generated text already exists. If so, it uses a fresh DB session to persist `progress_text`, `result_text`, `char_count`, and chapter `content_text` as `needs_review`, then returns instead of writing a failed status.
- Scope: generic cross-chapter/cross-project reliability fix for already-produced drafts. It does not add a post-output quality gate, does not introduce `needs_repair` as a main flow, and does not alter the prompt constraints or scene planner behavior.

2026-05-28 14:11:55 +0000
• 评分路径修复：evaluation 任务改走非流式 Chat Completions，并在章节评价侧将输出上限降到 1600，避免 OpenAI-compatible 代理 HTTP 200 后流式尾包长期不结束导致 Celery 评分任务卡在 running。该修复为通用评分路径，不绑定特定章节/项目。

2026-05-28 14:17:53 +0000
• 修复上一轮评分补丁的 _limit_text 字符串转义错误；评分链路保持短 JSON-only prompt、正文/大纲截断、evaluation 非流式 45s 超时和单次尝试。目标是跨章节/跨项目恢复评分可用性，避免后置质量门作为主流程。

## 2026-05-28T15:31:27+00:00 - Chinese prose mechanics checker diagnostics
- Added backend/app/services/chinese_prose_mechanics_checker.py as a reusable, diagnostic-only checker for cross-chapter Chinese prose mechanics.
- Measures: forbidden collocations, high-risk verb watchlist, short-sentence run max, long/repeated quote counts, physical-space watchlist hits, exposition cluster proxy, ornate verb density proxy.
- Purpose: feed measurable observations into the next generation prompt/self-check snapshot; it does not block generation, set needs_repair, or create a post-output hard gate.
- Verification: python3 -m py_compile backend/app/services/chinese_prose_mechanics_checker.py; smoke metrics for chapter_id=db18e301-38b2-4b47-8a93-e66472fee281 written to /tmp/chinese_prose_checker_ch5_v413_smoke_1779982287.json with no prose snippets.
- Next: wire summarized metrics into a v4.14 generation-before prompt snapshot, still force_direct/skip_scene_planner first.

## 2026-05-28T16:01:09+00:00 - v4.14 prose mechanics preflight snapshot landed
- Landed generation-before prose mechanics feedback for chapter generation in backend/app/tasks/knowledge_tasks.py.
- Landed build_generation_preflight_prompt() in backend/app/services/chinese_prose_mechanics_checker.py.
- Behavior: before a new draft, current stored chapter text is analyzed, latest completed evaluate_tasks.result_json issues are preferred, safe metrics are appended to user_instruction, and metadata is stored under params_json.chinese_prose_mechanics_preflight.
- Guardrail: diagnostic_only=true and hard_gate=false; it does not block generation, set needs_repair, start scoring, or create a post-output repair state.
- Verification: backend-container py_compile passed; safe smoke metadata for chapter_id=db18e301-38b2-4b47-8a93-e66472fee281 written to /tmp/preflight_container_smoke_1779984068.json without printing chapter prose.
- Next: run v4.14 force_direct/skip_scene_planner only after this wiring is restarted and health/ping pass.

## 2026-05-28T16:15:29.254277+00:00 - v4.14 prose mechanics checker result from monitor
- Behavior/result: v4.14 checker metrics: short_run=16 (delta 0), runs_over=25 (delta 0), verb_hits=19 (delta 0).
- Guardrail: diagnostic-only metrics; no hard gate, no needs_repair flow, no prose output.
- Related ids: task_id=be83bf79-c627-484e-b7fe-4a557b9c6698; chapter_id=db18e301-38b2-4b47-8a93-e66472fee281.
- Logs: /tmp/chinese_prose_checker_ch5_v414_monitor_1779984747.json

## 2026-05-28T23:10:35+00:00 - v4.14 final prose mechanics checker result
- Behavior/result: v4.14 final metrics: len=12791, short_run=12 (delta -4), runs_over=30 (delta 5), verb_hits=41 (delta 22), forbidden=0, long_quote=0, duplicate_quote=0.
- Guardrail: diagnostic-only metrics; no hard gate, no needs_repair flow, no prose output.
- Related ids: task_id=be83bf79-c627-484e-b7fe-4a557b9c6698; chapter_id=db18e301-38b2-4b47-8a93-e66472fee281.
- Logs: /tmp/chinese_prose_checker_ch5_v414_final_1780009834.json

## 2026-05-28T23:32:36+00:00 - v4.14 force_direct 持久化修复落地
- 目标：修复长耗时 force_direct 生成已产出正文但原 async session 在提交时连接关闭，导致 generation_tasks 有输出而 chapters 未落库的问题。
- 变更：在 `backend/app/tasks/knowledge_tasks.py` 中保留 primitive chapter id/volume/id/idx；当原 session 提交失败时，使用 fresh session 同步写入 `GenerationTask.progress_text/result_text/char_count/status` 和 `Chapter.content_text/word_count/status=needs_review`，并在 fresh-session 成功后直接返回，避免复用 stale session。
- 约束：不引入 post-output hard gate；不设置 needs_repair 作为主流程；不输出正文；保留 force_direct/skip_scene_planner 可用性。
- 验证：host 与 backend container `py_compile` 均通过；backend/celery 已重启，health 与 celery ping 已通过。
- 关联：task_id=be83bf79-c627-484e-b7fe-4a557b9c6698；chapter_id=db18e301-38b2-4b47-8a93-e66472fee281。

## 2026-05-28T23:35:47+00:00 - v4.15 prose mechanics density preflight targets
- Context: v4.14 landed safely but final diagnostics showed regressions in short-sentence run count and verb/action density while keeping forbidden collocations and long/duplicate quotes at zero.
- Change: upgraded the diagnostic generation-before Chinese prose mechanics prompt to v4.15. It now computes adaptive targets for `short_sentence_runs_over_target` and `verb_watchlist_hits`, adds a `rhythm_budget` reminder, and instructs drafts to reduce action slicing and reserve high-risk verbs for true contact/conflict points.
- Metadata: `params_json.chinese_prose_mechanics_preflight.version` is now `v4.15_generation_before_density_feedback` and includes diagnostic-only `generation_targets` ratios.
- Guardrail: this remains generation-before feedback only; no post-output hard gate, no needs_repair main flow, and no prose/prompt body is printed.
- Verification: host/container py_compile passed; safe smoke writes only metadata and metric subset.

## 2026-05-28T23:45:19.396163+00:00 - v4.15 final prose mechanics checker result
- Behavior/result: v4.15 final metrics: short_run=11, runs_over=24, verb_hits=11, forbidden=0, long_quote=0, duplicate_quote=0.
- Baseline: compared against v4.14 final metrics short_run=12, runs_over=30, verb_hits=41.
- Guardrail: diagnostic-only metrics; no hard gate, no needs_repair flow, no prose output.
- Related ids: task_id=3b523a90-d258-461f-956c-17952f2f743e; chapter_id=db18e301-38b2-4b47-8a93-e66472fee281.
- Logs: /tmp/chinese_prose_checker_ch5_v415_final_1780011587.json

## 2026-05-28T23:46:44.797859+00:00 - v4.15 interim mechanics metrics
- Behavior/result: task_status=needs_review; chapter content length checked via DB only; short_run=11, runs_over=24, verb_hits=11, forbidden=0, long_quote=0, duplicate_quote=0, exposition=4.
- Baseline: v4.14 final short_run=12, runs_over=30, verb_hits=41, exposition=1.
- Guardrail: diagnostic-only metrics; no hard gate, no needs_repair flow, no prose output.
- Related ids: task_id=3b523a90-d258-461f-956c-17952f2f743e; chapter_id=db18e301-38b2-4b47-8a93-e66472fee281.
- Logs: /tmp/chinese_prose_checker_ch5_v415_interim_1780012004.json

## 2026-05-28T23:47:32+00:00 - v4.16 generation-before exposition-density preflight patched
- Behavior/result: v4.16 keeps diagnostic-only generation-before feedback, preserves v4.15 action-density gains, and adds explicit exposition_budget targets for the v4.15 exposition_cluster_risk regression.
- Metrics basis: v4.15 final short_run=11, runs_over=24, verb_hits=11, exposition_cluster_risk=4.
- Guardrail: no post-output hard gate, no needs_repair flow, no prose output.
- Related ids: task_id=3b523a90-d258-461f-956c-17952f2f743e; chapter_id=db18e301-38b2-4b47-8a93-e66472fee281.
- Logs: /tmp/patch_v416_preflight_1780012052.log

## 2026-05-28T23:49:25.317368+00:00 - v4.16 preflight validation rerun passed
- Behavior/result: metadata=v4.16_generation_before_exposition_density_feedback; targets={'diagnostic_only': True, 'exposition_cluster_risk_max': 2, 'exposition_cluster_risk_ratio': 0.5, 'short_sentence_run_max': 4, 'short_sentence_runs_over_target_ratio': 0.75, 'verb_watchlist_hits_ratio': 0.85}; metrics={'duplicate_quote_segments_gt20': 0, 'exposition_cluster_risk': 4, 'forbidden_collocation_count': 0, 'long_quote_segments_gt80': 0, 'short_sentence_run_max': 11, 'short_sentence_runs_over_target': 24, 'verb_watchlist_hits': 11}; prompt_len=818; contains_exposition_budget=True.
- Validation: host/container py_compile OK; backend health OK; celery ping OK.
- Guardrail: diagnostic-only generation-before feedback; no hard gate, no needs_repair flow, no prose output.
- Related ids: task_id=3b523a90-d258-461f-956c-17952f2f743e; chapter_id=db18e301-38b2-4b47-8a93-e66472fee281.
- Logs: /tmp/rerun_validate_restart_v416_1780012139.log; smoke=/tmp/v416_preflight_smoke_fixed_1780012139.json; restart=/tmp/restart_backend_celery_v416_fixed_1780012139.log

## 2026-05-29T00:48:03+00:00 - v4.16 nested recovery mechanics result
- Task: e26954b2-45c5-4ca9-be54-05f14bd51ab4; chapter=db18e301-38b2-4b47-8a93-e66472fee281.
- Result: {"delta_vs_v415": {"exposition_cluster_risk": -4, "sentence_count": -40, "short_sentence_run_max": -2, "short_sentence_runs_over_target": -2, "verb_watchlist_hits": 12}, "metrics": {"duplicate_quote_segments_gt20": 0, "exposition_cluster_risk": 0, "forbidden_collocation_count": 0, "long_quote_segments_gt80": 0, "pass": false, "passed": false, "sentence_count": 858, "short_sentence_run_max": 9, "short_sentence_runs_over_target": 22, "space_watchlist_hits": 0, "verb_watchlist_hits": 23}}
- Note: diagnostic-only generation-before flow; no post-output hard gate; no prose output.
- Logs: /tmp/finalize_v416_nested_metrics_1780015682.log; checker=/tmp/chinese_prose_checker_ch5_v416_nested_final_1780015219.json

## 2026-05-29T01:51:08+00:00 - v4.17 prompt constraints validation
- Updated generation-before prompt constraints for noun construction, action downgrade, micro-measure bans, dialogue asymmetry, and few-shot conflict-flow principles.
- Validation: host/container py_compile OK; safe smoke OK; no post-output hard gate.
- Logs: /tmp/validate_v417_prompt_constraints_1780019468.log; smoke=/tmp/v417_preflight_smoke_1780019468.json

## 2026-05-29T01:52:14+00:00 - v4.17 full few-shot examples added
- Added user-specified complete contrast examples internally to the generation-before prompt.
- Validation: py_compile OK; safe boolean smoke OK; prompt/prose not printed.
- Logs: /tmp/patch_v417_full_fewshot_launch_1780019534.log


## 2026-05-29T05:46:41+00:00 v4.18 generation-before action/exposition reset
- Updated  to v4.18 diagnostic-only preflight.
- Tightened generation-before targets: short-run ratio 0.60, verb-watchlist ratio 0.60, exposition reset target 0.
- Added action_verb_budget and exposition_reset_budget instructions while preserving v4.17 negative list and few-shot contrast internally.
- Updated  metadata to .
- Validation: host/container py_compile passed; safe smoke passed without printing prose/prompt bodies.
- Next: restart backend/celery and launch v4.18 force_direct task.


## 2026-05-29T05:46:40+00:00 v4.18 generation-before action/exposition reset
- Updated backend/app/services/chinese_prose_mechanics_checker.py to v4.18 diagnostic-only preflight.
- Tightened generation-before targets: short-run ratio 0.60, verb-watchlist ratio 0.60, exposition reset target 0.
- Added action_verb_budget and exposition_reset_budget instructions while preserving v4.17 negative list and few-shot contrast internally.
- Updated backend/app/tasks/knowledge_tasks.py metadata to v4.18_generation_before_action_exposition_reset_feedback.
- Validation: host/container py_compile passed; safe smoke passed without printing prose/prompt bodies.
- Note: repaired this docs entry using Python to avoid shell backtick command substitution.
- Next: restart backend/celery and launch v4.18 force_direct task.


## 2026-05-29T05:48:25+00:00 v4.18 launch
- Restarted backend/celery after v4.18 code changes.
- Health check and Celery ping passed.
- Launched v4.18 force_direct async chapter generation with nested params.
- Task id: 869cc37c-d903-46cf-9e2c-bb0a674d3dfb
- Monitor log: /tmp/v418_safe_monitor_1780033686.log
- Checker target: /tmp/chinese_prose_checker_ch5_v418_final_1780033686.json
- No post-output hard gate; final metrics will be diagnostic only.


## 2026-05-29T05:56:23+00:00 v4.18 final diagnostics
- v4.18 task reached needs_review and backfilled chapter with 8412 chars.
- Final mechanics: short_sentence_runs_over_target=14, verb_watchlist_hits=23, exposition_cluster_risk=2, space_watchlist_hits=1.
- Compared with v4.17, short-run count improved by 12 and verb hits improved by 2; exposition risk stayed at 2.
- Compared with v4.16 nested, short-run count improved by 8 and verb hits were equal, but exposition risk regressed by 2.
- Next optimization: v4.19 should preserve v4.18 rhythm/action gains while resetting exposition risk to 0 and eliminating space watchlist hits.


## 2026-05-29T05:56:56+00:00 v4.19 generation-before exposition/space reset
- Updated backend/app/services/chinese_prose_mechanics_checker.py to v4.19 diagnostic-only preflight.
- Preserved v4.18 rhythm/action budget gains while adding paragraph_exposition_reset and space_watchlist zero-tolerance instructions.
- Updated backend/app/tasks/knowledge_tasks.py metadata to v4.19_generation_before_exposition_space_reset_feedback.
- Validation: host/container py_compile passed; safe smoke passed without printing prose or full prompt bodies.
- Next: restart backend/celery and launch v4.19 force_direct task.


## 2026-05-29T05:57:38+00:00 v4.19 launch
- Restarted backend/celery after v4.19 code changes.
- Health check and Celery ping passed.
- Launched v4.19 force_direct async chapter generation with nested params.
- Task id: f77e0e3c-b0ad-4dc0-8fc3-84bb208db69e
- Monitor log: /tmp/v419_safe_monitor_1780034240.log
- Checker target: /tmp/chinese_prose_checker_ch5_v419_final_1780034240.json
- No post-output hard gate; final metrics will be diagnostic only.

## 2026-05-29T06:13:01+00:00 v4.20 recovered launch
- Recovered after the previous combined v4.20 script failed while reading a non-UTF-8 byte in root PROGRESS.md.
- v4.20 code markers and host/container py_compile are valid.
- Proceeding with restart and v4.20 force_direct launch.

## 2026-05-29T06:13:17+00:00 v4.20 launch
- Restarted backend/celery and launched v4.20 force_direct task.
- Task id: 8af1804a-2176-41e5-8829-bb1fbae9a2ef
- Monitor log: /tmp/v420_safe_monitor_1780035180.log
- Checker target: /tmp/chinese_prose_checker_ch5_v420_final_1780035180.json
- No post-output hard gate; checker remains diagnostic-only.


## 2026-05-29T06:21:01.696081+00:00 v4.20 终态机械指标总结
- task_id: 8af1804a-2176-41e5-8829-bb1fbae9a2ef
- chapter_id: db18e301-38b2-4b47-8a93-e66472fee281
- status: needs_review; chapter 已回填；checker_json=/tmp/chinese_prose_checker_ch5_v420_final_1780035180.json
- result: sentence_count=847; short_sentence_run_max=15; short_sentence_runs_over_target=28; verb_watchlist_hits=42; exposition_cluster_risk=2; space_watchlist_hits=0; pass=False
- deltas: vs v4.19 exposition_cluster_risk -4, but short_sentence_runs_over_target +15 and verb_watchlist_hits +22; vs v4.18 exposition unchanged, action/rhythm regressed.
- interpretation: v4.20 修复一部分说明聚集并保持空间词为 0，但动作/短句密度明显反弹；下一轮应回滚 v4.20 过强扩写倾向，采用 v4.21 “动作/节奏硬目标写前约束”，保留说明拆分但降低动作动词预算。
- next: patch v4.21 generation-before preflight; no post-output 硬门禁；继续 force_direct_chapter/skip_scene_planner。
- Todo:
  - [x] v4.20 落库与回填确认
  - [x] v4.20 机械指标检查
  - [ ] v4.21 写前约束补丁
  - [ ] v4.21 短批生成验证


## 2026-05-29T06:21:59.298442+00:00 v4.21 写前约束补丁
- action: patched generation-before preflight from v4.20 to v4.21 rhythm/action clamp.
- result: metadata version v4.21_generation_before_rhythm_action_clamp_feedback; reduced short_sentence_runs_over_target_ratio to 0.45 and verb_watchlist_hits_ratio to 0.35; kept exposition split order and space zero-tolerance; no post-output hard gate.
- validation: host/container py_compile passed; safe smoke verifies v4.21 markers without printing prompt/prose.
- next: restart backend/celery, verify health/celery, then launch v4.21 force_direct chapter generation.


## 2026-05-29T06:23:26.930518+00:00 v4.21 已启动
- task_id: e27769e0-05dc-4059-8bf5-626943884441
- chapter_id: db18e301-38b2-4b47-8a93-e66472fee281
- payload: /tmp/ch5_v421_rhythm_action_payload_1780035795.json
- response: /tmp/launch_v421_rhythm_action_1780035795.json
- monitor: /tmp/v421_safe_monitor_1780035795.sh
- monitor_log: /tmp/v421_safe_monitor_1780035795.log
- checker_json: /tmp/chinese_prose_checker_ch5_v421_final_1780035795.json
- result: async generation launched; force_direct=true; skip_scene_planner=true; fallback_timeout_seconds=1200; no post-output hard gate.
- next: poll monitor and summarize v4.21 metrics when terminal.

## 2026-05-29T06:37:00+00:00 v4.21 terminal mechanics result
- task_id: e27769e0-05dc-4059-8bf5-626943884441
- chapter_id: db18e301-38b2-4b47-8a93-e66472fee281
- result: completed, chapter backfilled as needs_review, 3819 chars.
- diagnostic-only checker:
  - sentence_count=283
  - short_sentence_run_max=6
  - short_sentence_runs_over_target=2
  - verb_watchlist_hits=16
  - exposition_cluster_risk=3
  - space_watchlist_hits=0
  - forbidden_collocation_count=0
  - long_quote_segments_gt80=0
  - duplicate_quote_segments_gt20=0
  - pass=false, passed=false
- interpretation: v4.21 reclaimed rhythm/action density strongly while preserving zero space-watchlist hits, but left 3 exposition cluster proxies and over-shortened output length. Next prompt/config direction is v4.22: keep v4.21 clamps, add length-floor guidance and stricter paragraph-level exposition split without post-output hard gate.

## 2026-05-29T07:11:50+00:00 v4.22 retry result: exposition fixed, rhythm regressed
- task_id: 5a8d6f02-bed1-4f03-8b4e-80250a781b6b
- chapter_id: db18e301-38b2-4b47-8a93-e66472fee281
- result: v4.22 retry completed to needs_review with 7394 chars. Generation-before exposition/length feedback recovered length and reduced exposition_cluster_risk to 0, paragraph_risk_count to 0, while preserving space/forbidden/long-quote/duplicate-quote zero hits.
- metrics: sentence_count=646; short_sentence_run_max=17; short_sentence_runs_over_target=28; verb_watchlist_hits=17; exposition_cluster_risk=0; space_watchlist_hits=0; passed=false.
- comparison: vs v4.21, length +3575 chars and exposition risk -3, but short-sentence run max +11 and over-target runs +26.
- next: v4.23 should combine v4.22 exposition/length constraints with stricter v4.21-style rhythm clamp, still diagnostic/generation-before only, no hard gate.

## 2026-05-29T07:12:57+00:00 v4.23 generation-before rhythm reclamp patch
- related_task_id: 5a8d6f02-bed1-4f03-8b4e-80250a781b6b
- chapter_id: db18e301-38b2-4b47-8a93-e66472fee281
- change: v4.23 keeps v4.22 exposition split and length-recovery direction, while tightening the generation-before rhythm target back toward v4.21.
- metadata: version=v4.23_rhythm_reclamp_exposition_length_feedback; short_sentence_runs_over_target_ratio=0.25; short_sentence_run_max_target=6; v4_23_rhythm_reclamp_from_v4_21=true; keep_v4_22_exposition_length_recovery=true.
- validation: host py_compile passed for knowledge_tasks.py and chinese_prose_mechanics_checker.py.
- next: sync to containers, restart backend/celery, then launch a short v4.23 force-direct retry.

## 2026-05-29T07:30:08+00:00 v4.23 rhythm reclamp result
- Behavior result: generation-before rhythm reclamp restored sentence rhythm strongly but introduced zero-tolerance regressions.
- task_id: 2eaa3347-9299-4886-9d5d-d4eaf7039d7c
- chapter_id: db18e301-38b2-4b47-8a93-e66472fee281
- Metrics: chars=4758; sentence_count=252; short_sentence_run_max=3; short_sentence_runs_over_target=0; verb_watchlist_hits=17; exposition_cluster_risk=1; paragraph_risk_count=2; space_watchlist_hits=1; forbidden_collocation_count=1; long_quote_segments_gt80=0; duplicate_quote_segments_gt20=0; passed=false.
- Interpretation: v4.23 overcorrected rhythm successfully while partially losing v4.22 exposition/zero-tolerance stability. Next behavior change should keep v4.23 rhythm constraints, restore strict space/forbidden-collocation zeroing, and add a length-support clause that uses environment state and consequence beats rather than micro-action chains.

## $(date -Is) v4.24 checker patch repaired
- Behavior change completed: checker preflight prompt now emits v4.24 wording alongside metadata already patched in knowledge_tasks.py.
- Validation: host py_compile passed for knowledge_tasks.py and chinese_prose_mechanics_checker.py.
- Safety: generation-before diagnostic only; no post-output hard gate added.

## $(date -Is) v4.25 constraint priority patch
- Behavior change: v4.25 adds explicit constraint priority order to avoid v4.23/v4.24 oscillation: zero-tolerance first, short-sentence rhythm second, exposition split third, action-result-only fourth, dialogue asymmetry fifth, length last.
- Conflict rule: length yields to rhythm and zero-tolerance; no short-sentence chains, micro-action trajectory, or invented official nouns may be used to recover length.
- Validation: host py_compile passed for knowledge_tasks.py and chinese_prose_mechanics_checker.py.
- Safety: generation-before diagnostic feedback only; no post-output hard gate added.

## v4.26 no-length-pressure patch
- Behavior change: after v4.25 regressed to over-length and short-sentence chains, v4.26 disables length pressure and makes zero-tolerance/rhythm dominant.
- Priority: forbidden/space zero first; short-sentence rhythm second; exposition split third; length last and allowed to yield.
- Soft range adjusted to 5200-6500 characters; no scene expansion for length.
- Validation: host py_compile passed for knowledge_tasks.py and chinese_prose_mechanics_checker.py.
- Safety: generation-before diagnostic feedback only; no post-output hard gate added.

## 2026-05-29T12:53:43+00:00 v4.27 rhythm-only generation-before patch
- Change: added v4.27 rhythm-only constraints after v4.26 completed with acceptable length/zero-tolerance improvement but worse short-sentence chains.
- Scope: backend/app/tasks/knowledge_tasks.py and backend/app/services/chinese_prose_mechanics_checker.py.
- Intent: keep no length pressure and force direct generation, but explicitly prevent comma-chopped short-clause chains by requiring medium causal/transition sentences.
- Validation: host py_compile passed.
- Next: sync patched files into backend/celery containers, restart, verify health and Celery ping, then launch a short v4.27 force-direct run.


## v4.28 rhythm clamp patch
- Change: After v4.27 regressed on length and short-sentence chains, the generation-before mechanics prompt now restores a strict rhythm clamp and removes scene-expansion incentives.
- Intent: keep force-direct/no-hard-gate flow while prioritizing sentence merging, trimming expanded scene detail, and preserving zero-tolerance constraints.
- Validation: host py_compile is run in the patch step; container sync/restart remains a separate step.


## 2026-05-29T13:19:06.714047+00:00 v4.29 zero-tolerance-before-rhythm patch
- task_id: 80f071e4-b503-4e4f-a27c-002b857f73d6
- chapter_id: db18e301-38b2-4b47-8a93-e66472fee281
- result: v4.28 improved rhythm but regressed forbidden/space/long quote; v4.29 reorders generation-before guidance to zero-tolerance lexical cleanup before rhythm/length.
- next: py_compile, sync containers, restart backend/celery, verify imports/health/ping, then launch short v4.29 retry if requested.


## 2026-05-29T13:52:35.027467+00:00 v4.30 small alignment patch
- task_id: 506115bc-ebcc-4c0c-b2e4-0cc5258ed672
- chapter_id: db18e301-38b2-4b47-8a93-e66472fee281
- v4.29 completed at 5639 chars with good rhythm/quote metrics but failed zero-tolerance counts.
- v4.30 removes the canon/title false positive from forbidden counting and emphasizes the remaining micro-measure terms before generation.


## 2026-05-29T14:08:19.426843+00:00 v4.31 residual lexical/exposition patch
- task_id: 0c0d420e-cbdf-4069-af95-8258a1804e40
- chapter_id: db18e301-38b2-4b47-8a93-e66472fee281
- v4.30 reached needs_review at 3664 chars with rhythm/quote metrics improved, but still had one residual micro-measure term and one exposition cluster.
- v4.31 keeps the direct generation-before path and adds residual lexical zeroing plus repeated-role/exposition dispersion guidance.


## 2026-05-29T14:17:06.061280+00:00 v4.32 length/exposition clamp patch
- task_id: f04cb639-1160-4eaa-ba26-cfb6cb2e9437
- chapter_id: db18e301-38b2-4b47-8a93-e66472fee281
- v4.31 zeroed space/forbidden terms but expanded to 8323 chars and still had exposition/paragraph risk.
- v4.32 keeps lexical zero as baseline and tightens generation-before instructions around length rollback, repeated role terms, and exposition-cluster cleanup.


## v4.33 generation-before lexical/length clamp (2026-05-29T14:31:51.793451Z)
- v4.32 completed but failed mechanical pass: chars=8336, short runs fixed, but 半寸/半步 regressed, exposition_cluster_risk=1, paragraph risk remained.
- Added generation-before-only v4.33 instruction: internal pre-return self-check for zero micro-measure terms, 5200-6200 target, delete whole exposition blocks over 6500, no scene expansion for length, no post-output hard gate.


## v4.34 concise-rewrite generation-before patch
- v4.33 completed but failed mechanical pass: chars=9499, short run risk returned, one long quote segment appeared, exposition/paragraph risks remained, and role terms were still dense.
- Added v4.34 generation-before concise-rewrite guidance: target 4800-5800 chars, delete over-6200 material before writing more, keep micro-measure terms at zero, ban long quote segments, reduce repeated role-term density, and avoid scene/exposition padding.
- Safety: generation-before diagnostic feedback only; no post-output hard gate added.


## v4.35 rhythm + lexical-zero generation-before patch
- Applied a minimal generation-before update after v4.34 completed at 5538 chars but failed mechanical pass on short-sentence runs and one residual micro-measure term.
- Direction: keep v4.34 length/exposition gains, merge staccato short sentences into medium causal/environmental sentences, and keep all micro-measure/trajectory terms at zero without adding a post-output hard gate.
- Changed checker prompt marker to v4.35 and knowledge task metadata to v4.35_rhythm_lexical_zero_feedback.


## v4.36 lexical-zero-first length/rhythm generation-before patch
- Applied a minimal generation-before update after v4.35 reached needs_review at 4139 chars but still failed on short-sentence runs and residual micro-measure terms.
- Direction: prioritize lexical zero before rhythm, keep length in a 5200-5600 soft range with a 5000 lower-bound guard, and keep exposition/long-quote/paragraph gains without adding a post-output hard gate.
- Changed checker prompt marker to v4.36 and knowledge task metadata to v4.36_lexical_zero_first_length_rhythm_feedback.

## narrative anti-mechanical flow doc
- Added `docs/narrative_antimechanical_flow.md` as a reusable process distilled from Chapter 5 issues.
- Scope: dialogue symmetry, motion-calculation action, stage-direction narration, and game-item clue exposition.
- This is a generation-before process document only; no hard gate or new generation was launched.

## 2026-05-30 01:38:34 +0000 v4.10+ chinese_prose_mechanics_checker 定型
- 新增 backend/app/services/chinese_prose_mechanics_checker.py：将中文行文机械约束从「只在 prompt 里表述」升级为「可检测、可量化、可持续、跨章节/跨项目通用」的 analyzer
- 输出指标（全部只输出计数/枚举/段落索引，绝不输出正文片段）：
  - forbidden_collocation_count/forbidden_collocation_counts：供纸案匣、外目三、手腕一翻、从肘下钻过、半寸、半息、半指 等明确禁例
  - space_watchlist_hits：半寸/半息/半指/半步/寸许/尺许/肘下/腇下
  - verb_watchlist_hits：一翻/钻过/掠出/探手/反手/肘下/腇下/腕/膝/踝/贴着/错身/旋身 等肢体轨迹词
  - sentence_count / short_sentence_run_max / short_sentence_runs_over_target（中文标点切句，≤8 字为短句，连发目标 ≤4）
  - exposition_cluster_risk / paragraph_risks：段内制度/规制/衡门/名册/卷宗/供奉/守备/门人/账书/香火 等词频 ≥8 且段长 >220
  - long_quote_segments_gt80 / duplicate_quote_segments_gt20
  - passed（诊断位，不作为任何主流程硬门）
- 提供双接口：
  - analyze_chinese_prose_mechanics(text) -> ChineseProseMechanicsReport
  - analyze_to_safe_dict(text) -> dict
  - dumps_report(report) -> str
  - build_generation_preflight_prompt(report, *, issue_focus, version_label) -> str（主调用者拼接进生成前 system prompt 末尾，作为生成前自检清单）
- 接入原则（后续接入 narrative_quality_gates / knowledge_tasks force_direct 路径时必须遵守）：
  - 只用于诊断与生成前提示，不作为 needs_repair / hard gate 上游触发器
  - 不阐断 fallback / force_direct 直出路径
  - 不允许造成 0 字卡死
- 首次 smoke（第五章 v4.13，task_id 2240fbec-...）指标：
  - sentence_count=361，short_sentence_run_max=7，short_sentence_runs_over_target=7（目标 ≤4）
  - forbidden_collocation_count=22；供纸案匣×6、外目三×7、手腕一翻×1、半寸×4、半息×2、半指×2
  - space_watchlist_hits=9，verb_watchlist_hits=18
  - exposition_cluster_risk=1 (paragraph_1)；passed=false
- 意义：验证 prompt-only 约束仍不足以清零微观刻度词与生造名词；v4.14 需把 checker 报告带入生成前 prompt，让 LLM 在下一稿内化

## 2026-05-30 v4.36 chinese_prose_mechanics 可检测化 + 生成前提示注入定型
- 模块：backend/app/services/chinese_prose_mechanics_checker.py
  - analyze_chinese_prose_mechanics(text) -> 指标：forbidden_collocation（供纸案匮/外目三/手腕一翻/从肘下钻过/半寸/半息/半指）、space_watchlist、verb_watchlist、短句连发 run_max/over_target（目标 ≤4）、exposition_cluster_risk、长引(>80)/重复引(>20)
  - to_safe_dict / build_generation_preflight_prompt（v4.36：先词汇清零再节奏合并，5200-5600 篇幅，连续短句≤4，exposition_cluster_risk=0）
- 接入：backend/app/tasks/knowledge_tasks.py
  - 新增 async _build_chinese_prose_preflight_prompt(ch, db)：用本章存稿 + 最近 completed eval issues 生成“生成前自检提示”+ meta
  - chapter 生成路径（含 force_direct 直出同链路）把 preflight 文本拼接进 user_instr；meta 落 task.params_json.chinese_prose_mechanics_preflight
  - diagnostic_only=true, hard_gate=false：仅诊断/观测/生成前提示，不阻断、不触发后置修订
- 性质：跨章节/跨项目通用中文行文机械约束；用于第一稿内化，不作为 hard gate / needs_repair
- 验证：py_compile 通过；backend/celery 重启正常（/api/health ok、celery pong）；smoke 对第五章确认 prompt 注入生效（prompt_len=535、diagnostic_only/hard_gate 标志正确）

## [2026-05-30 02:30:08 UTC] v4.36 行为修复: 生成前 issue_focus 过滤评分链路占位标签
- 变更文件: backend/app/tasks/knowledge_tasks.py (函数 _build_chinese_prose_preflight_prompt)。
- 行为变更: 中文行文机械约束的「生成前 issue-aware 关注项」(issue_focus) 现在会忽略评分失败兜底产生的占位标签 (dimension="system", overall=0)，并在最近 5 条 completed EvaluateTask 中优先选取真实评分(overall>0)结果，必要时回退 ChapterEvaluation；标签保序去重。
- 影响: build_generation_preflight_prompt 的 issue_focus 由退化的 ["system"] 恢复为真实违规类型(如 space_clarity / exposition_cluster / overpointed_clue 等)，提升生成前内化提示的针对性。
- 不变量: 仍为 diagnostic_only / 非 hard_gate, 不引入任何后置质量门或 needs_repair 状态; force_direct 直出路径不受影响。
- 验证: py_compile OK; backend+celery 重启正常; /api/health ok; celery pong; smoke 显示 issue_focus 含 10 条真实标签, prompt_len=704, diagnostic_only=true, hard_gate=false。

## 2026-06-02 — 跨项目正文质量内化

- 新增 `docs/narrative_prose_quality_contract.md`，把《神裔》第一章反馈沉淀为跨项目规则：伪文学压缩腔、正常汉语搭配缺失、生活逻辑与资源链断裂、重复解释、对话过度聪明。
- 新增 `backend/app/services/prose_quality_rules.py` 作为规则单一来源，供生成前提示、质量门禁、检查器和人工审核共用。
- 新增聚合指标 `plain_contemporary_violation_count` 与 `duplicate_explanation_span_count`，防止只修用户点名句子。
- `/api/chapters/{chapter_id}/check-quality` 返回 `chinese_prose_mechanics`，前端检查面板可人工审核行文机械问题。

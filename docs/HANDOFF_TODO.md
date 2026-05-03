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

## feat/neo4j-batch1 — PR-NEO1~NEO4 v2.0 批次 已 commit（2026-05-03，未 push）

HEAD = `fe01405`。详情看 `docs/PROGRESS.md` 同日条目。

- [x] PR-NEO1 道具节点+持有/转移事件（`fa82700`）
- [x] PR-NEO2 阵营事件+对立窗口（`932831a`）
- [x] PR-NEO3 时间锚点+章节绑定（`6d21591`）
- [x] PR-NEO4 ContextPack 注入+critic item_names/chapter_idx 通线（`fe01405`）
- [x] alembic 链推进至 `a1001911`，COMPILE_OK 全过
- [ ] **下一步（任务 A 重跑时一并验证）**：
  - [ ] 用 PID `310c1f9a` 或新 PID 跑 30 章全流程，确认每章 entity 抽取后 PG 三对新表（`items` / `item_events` / `faction_events` / `faction_event_orgs` / `faction_oppositions` / `time_anchors` / `chapter_time_anchors`）有 row count > 0
  - [ ] 直接 print 一次 ContextPack.to_system_prompt() 验证三新块（「当前道具持有」 / 「阵营动态」 / 「时间锚点 v2」）出现
  - [ ] 校验 critic_reports 中 `consistency:item_missing` / `consistency:time_reversal` / `consistency:geo_jump` issue 出现率（>0 即说明 checker 不再 NOOP）
  - [ ] 朱雀 AI% 与 baseline 12.04% / 42.21% / 45.75% 对比
- [ ] **后续 PR-NEO5（不在本批）**：三个 checker severity 校准 + 单测；ContextPack 三新块的 token-budget 截断；recent_events 排序优化（按相关性而非纯时间倒序）。

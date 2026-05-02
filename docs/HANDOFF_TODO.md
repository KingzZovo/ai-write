# Handoff TODO（可直接打勾执行）

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

- [x] 参考书 status=ready (《龙族全套》/《天之炽》/《天之炽②女武神》)，qdrant slice/style/plot/embedding collection 点亮 ✅
- [x] 创建项目 `0eaeff87-2f91-452c-812c-b4bcf2924fe2` ✅
- [x] 书级大纲×1 (`f9af7582...` 6766 chars)、已 confirm ✅
- [x] 分卷大纲×3（并发 60-70s/卷）✅
- [x] 物化 PG：3 volumes + 30 chapters，每章携 outline_json ✅
- [x] 生成 30 章正文：30/30 done、277,882 字、avg 9263、min 7377、max 13155（4-worker 并发 ~27 min）✅
- [x] 抽样质量：v1.c1/v2.c5/v3.c10 人物跨卷一致、剧情连贯、「中二热血×现代都市」调性达标 ✅
- [x] 修复 `chapter_generate_stream` v0.5+ 签名不匹配 → PR #21 (+9/-3) 已提交 ✅
- [ ] **后续**：章节生成后未自动触发 entity 抽取/cascade/evaluation（`characters=0, foreshadows=0, cascade_tasks=0`）——需手动调 `entities.extract_chapter` celery task，或为 `_run_async_generation_impl` chapter 分支加生成后钩子
- [ ] **后续**：加 `tests/tasks/test_run_async_generation_chapter.py` mock ChapterGenerator 锁定签名防回归

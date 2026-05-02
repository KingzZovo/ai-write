# Handoff TODO（可直接打勾执行）

> 最近一次更新：2026-05-02 晚 — main HEAD = `0a4f9a1` (PR #18 合并)。**所有 P0~P4 + 之前 follow-up 全部关闭**，新增 P5 部署 checklist。**新窗口接手优先看 `docs/PROGRESS.md`**。

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

## P5 部署（新窗口可执行） ⏳ 未开始
- [ ] `alembic upgrade head` → target `a1001908_v190_character_organizations_table`
- [ ] 配置 env `ADMIN_USERNAMES`（JSON 数组，例 `["admin","king"]`）；缺失时 `/api/admin/*` 路由族对所有 JWT-sub 返回 403
- [ ] 启动后端 + Celery worker + 前端
- [ ] 烟测：写 `POST /api/projects/{pid}/neo4j-settings/foreshadows` → Neo4j `(:Foreshadow)` 出现 → PG `foreshadows` 表 materialize 出同一行
- [ ] 烟测：`POST /api/admin/entities/materialize` 返回 200 + 计数指标
- [ ] 真实环境跑 `verify_entity_writeback_v19.sh`，结果回填到部署 PR

## P6 仓库清理（可选，非阻塞）
- [ ] 关闭 / 删除 `origin/feature/v1.0-big-bang`（其内容已 100% 在 main）
- [ ] 关闭 / 删除 11 个 `origin/release/v1.*` 分支（已 merge）
- [ ] 如要继续推进 v2.0+，参考 `ITERATION_PLAN.md` Iteration 系列

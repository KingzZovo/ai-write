# 项目当前进展（持续维护）

> 目的：让任何新窗口/新同学**只看这一份**就能知道：已经做了什么、现在做到哪、下一步做什么、怎么验收。

## 0. 一句话架构结论（TL;DR）

- **Neo4j 是设定集真相源（source of truth）**；Postgres 仅为读优化投影（materialize）。
- 任何设定集实体写入：**写 Neo4j → materialize → PG**；禁止 PG 直写以避免 drift。
- 例外：`foreshadows` 当前仍 PG 直写，路线决策见下方 P2。

## 1. 最近一次更新

- 日期：2026-05-01
- 更新人：自动执行代理（GitHub MCP，无本地 shell）
- 关联 PR：本 PR（`chore/runbook-and-handoff-sync`）

## 2. 已完成（按时间倒序）

- [x] 2026-05-01 — 补齐 `docs/RUNBOOK.md`（写入入口 / Anti-pattern / 410 表 / 验收命令 / 灾难恢复 / PR 模板）（本 PR）
- [x] 2026-05-01 — `.gitignore` 补产物目录排除：`.audit-*/`、`backups/`、`backend/.coverage`、`.coverage*`、`*.sse`（本 PR）
- [x] 2026-05-01 — P2 远端代码扫描（GitHub `search_code`，跨整库）：
  - `INSERT/UPDATE/DELETE INTO (world_rules|relationships|locations|foreshadows)` = **0 命中**
  - `WorldRule(` / `db.add WorldRule` / `db.add Relationship` = **0 命中**
  - 但代码审阅发现：`backend/app/api/foreshadows.py` 仍直接 `db.add(Foreshadow(...))`、`db.delete(foreshadow)`，未走 Neo4j 链路 → 列入 P2 follow-up
- [x] PR #3 — 禁用 legacy `/world-rules`、`/relationships` 写接口（→ 410），引导到 `/neo4j-settings/*`
- [x] PR #2 — README + ITERATION_PLAN 文档持续维护
- [x] PR #1 — v1.9 主要收敛（outlines extract / world_rules ETL / relationships deletion sync 等）

## 3. 进行中（正在做 / 卡点）

- [ ] **P0 本地状态清理**（仅本机可执行）
  - 当前状态：当前会话只有 GitHub MCP 与 AWS MCP，**没有本地 shell**；P0（`git fetch / reset / stash / status / compileall`）必须在用户 `/root/ai-write` 上执行
  - 阻塞点：远端代理无 shell 执行权限
  - 下一步动作：用户照 RUNBOOK §4.1–4.3 跑完，把 `git status` 与 `git log -1 --oneline` 输出粘到本节并打勾 HANDOFF_TODO P0

- [ ] **P2 残留：foreshadows PG 直写迁移决策**
  - 当前状态：`backend/app/api/foreshadows.py` 仍是纯 PG ORM 写（POST 创建 / PUT 更新 / DELETE 删除 / POST resolve）
  - 阻塞点：需要决策 Foreshadow 是否纳入 Neo4j 真相源链路
  - 下一步动作：开 follow-up PR
    - 选项 A：新增 `/neo4j-settings/foreshadows` 写入口 + materialize 投影回 `foreshadows` 表，把现有 `/foreshadows` 写接口改 410
    - 选项 B：在 RUNBOOK §3 显式声明 foreshadows 不入 Neo4j 真相源链路，并加回归测试避免别处误用

## 4. 下一步（可执行清单，按优先级）

### P0（必须，仅本机执行）
- [ ] `git fetch origin main && git reset --hard origin/main && git status` 干净
- [ ] `git stash list` 检查；如需要仅恢复单个 md，确认无用后 `git stash drop`

### P1（应该）
- [x] 补齐 `docs/RUNBOOK.md`（本 PR）
- [ ] 用户本地 `python -m compileall -q backend/app` 通过

### P2（应该）
- [ ] foreshadows 路线决策（见 §3）并落地 follow-up PR
- [ ] 用户本地复核 `grep -RnE "INSERT INTO (world_rules|relationships|locations|foreshadows)" backend/app` = 0 行（远端 search_code 已为 0）

### P3（可选）
- [ ] 用户本地：`PROJECT_ID=... CHAPTER_IDX=... bash scripts/verify_entity_writeback_v19.sh`（脚本若已就位）

## 5. 验收 / 回归验证（复制即用）

详见 [docs/RUNBOOK.md §4](RUNBOOK.md)。

## 6. 文档更新规则（必须遵守）

每个可合并的 PR（一个"步子"）都必须同步更新文档，确保可交接：

- 影响**怎么做/怎么验收/怎么运维**：更新 `docs/RUNBOOK.md`
- 影响**架构结论/推荐入口/对外行为**：更新 `README.md`
- 影响**里程碑与计划**：更新 `ITERATION_PLAN.md`
- 跨窗口交接：更新 `docs/HANDOFF_EXECUTION.md` + `docs/HANDOFF_TODO.md`
- 当前进展快照：更新 `docs/PROGRESS.md`（本文件）

PR 描述必须包含：Context / Change / Verification / Docs updated。

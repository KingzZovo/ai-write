# Handoff (执行版) — Neo4j Truth Source 收尾与回归验证

> 目的：把"设定集（settings entities）Neo4j 真相源 + Postgres 投影"的改造收口做干净，让新窗口/新同学只看本文即可接手继续推进。

## 0. 当前结论（必须记住）

- **Neo4j 是真相源**：`world_rules / relationships / locations / character_states` 等设定集实体，所有写入必须先落 Neo4j。
- **Postgres 是 read model**：PG 只保存从 Neo4j materialize 出来的投影数据，用于读优化。
- **禁止 PG 直写**：避免 drift。
- 例外：`foreshadows` 仍 PG 直写，路线待决策（详见 §7 与 docs/PROGRESS.md）。

### 已完成并已合并的关键 PR

- PR #1：v1.9 主要收敛（outlines extract / world_rules ETL / relationships deletion sync 等）
- PR #2：README + ITERATION_PLAN 文档持续维护
- PR #3：禁用 legacy PG 直写接口（`/world-rules`、`/relationships` 的 POST/PUT/DELETE → **410**，引导 `/neo4j-settings/*`）
- 本 PR（`chore/runbook-and-handoff-sync`）：补齐 `docs/RUNBOOK.md` + 同步 PROGRESS/HANDOFF + 补 `.gitignore` 产物目录

---

## 1. 开工第一步：把本地状态清到可重复（P0）

> 目标：本地 `main` 与 `origin/main` 一致、工作区干净、无产物目录污染。

### 1.1 同步 main（强制推荐做法）

```bash
cd /root/ai-write

git fetch origin main
# 如本地无价值脏改动，直接硬重置（最省事，最稳）
git reset --hard origin/main

git status
```

验收：
- `git status` 显示 `working tree clean`
- `git log -1 --oneline` 与 `origin/main` 的 HEAD 一致

### 1.2 如存在 stash（之前为拉取更新做过 stash -u）

```bash
git stash list
# 仅恢复需要的文件：
# git checkout stash@{0} -- README.md
# git checkout stash@{0} -- ITERATION_PLAN.md
```

确认无用后清理：
```bash
git stash drop stash@{0}
# 或清空：
# git stash clear
```

### 1.3 产物目录防误提交

（已在本 PR 的 `.gitignore` 里加上）
- `.audit-*/`
- `backups/`
- `backend/.coverage`、`.coverage*`
- `*.sse`

---

## 2. 必做收尾：RUNBOOK 补齐（P1）

> ✅ 已在本 PR 完成，详见 `docs/RUNBOOK.md`。任何人不会再误用 PG 直写。

包含：
- 唯一正确写入路径：**write Neo4j → materialize → PG**
- 常用写入口：`POST /api/projects/{project_id}/neo4j-settings/*`、`POST /api/projects/{project_id}/outlines/{outline_id}/extract-settings`
- 手动 materialize：`POST /api/admin/entities/materialize`
- Legacy 接口现状：`/world-rules`、`/relationships` 的写接口返回 **410**；`/foreshadows` 当前仍 PG 直写（待决策）

---

## 3. 防回归：全仓扫描是否还有 PG 直写（P2）

> 目标：从代码层面把 drift 的可能性清零。

### 3.1 远端 search_code 结果（本 PR 时）

所有以下查询在整仓扫描结果均为 **0 命中**：

- `INSERT INTO world_rules` / `INSERT INTO relationships` / `INSERT INTO locations` / `INSERT INTO foreshadows`
- `UPDATE world_rules` / `UPDATE relationships SET` / `DELETE FROM world_rules` / `DELETE FROM relationships` / `DELETE FROM foreshadows`
- `db.add WorldRule` / `db.add Relationship` / `WorldRule(` (extension:py)

### 3.2 本地复核（一次性脚本）

```bash
cd /root/ai-write

grep -R --line-number -E "INSERT INTO (world_rules|relationships|locations|foreshadows)" backend/app || true
grep -R --line-number -E "UPDATE (world_rules|relationships|locations|foreshadows)" backend/app || true
grep -R --line-number -E "DELETE FROM (world_rules|relationships|locations|foreshadows)" backend/app || true
grep -R --line-number -E "\\bWorldRule\\(" backend/app || true
grep -R --line-number -E "\\bRelationship\\(" backend/app || true
grep -R --line-number -E "db\\.add\\(.*(WorldRule|Relationship|Location)" backend/app || true
```

### 3.3 已知 P2 缺口（必须 follow-up）

- `backend/app/api/foreshadows.py`：POST/PUT/DELETE/resolve 仍直接 `db.add(Foreshadow(...))` / `db.delete(foreshadow)` / `setattr(foreshadow, ...)`。
- 处理策略二选一：
  - **选项 A**：新增 `/neo4j-settings/foreshadows` 写入口 + materialize 投影；把现有 `/foreshadows` 写接口改 410。
  - **选项 B**：在 RUNBOOK §3 显式声明 `foreshadows` 不入 Neo4j 真相源链路；加回归测试避免别处误用。

---

## 4. E2E 验收（P3）

### 4.1 Python 语法/编译检查

```bash
cd /root/ai-write
python -m compileall -q backend/app
```

### 4.2 运行对账脚本（若存在）

```bash
cd /root/ai-write
PROJECT_ID=<your_project_id> CHAPTER_IDX=0 \
  bash scripts/verify_entity_writeback_v19.sh
```

验收：脚本输出 OK；Neo4j → materialize → PG 的数据一致。

---

## 5. 交付物要求（你每次 PR 必须满足）

- PR 描述必须 4 段式：Context / Change / Verification / Docs updated
- 避免提交：`.audit-*`、`backups/`、`.coverage`、临时 sse 文件等（已在 `.gitignore`）

---

## 6. 下一步建议顺序（直接照做）

1. P0：本地 reset 到 `origin/main`、清 stash、清产物（仅本机）
2. P1：✅ 本 PR 已补齐 `docs/RUNBOOK.md`
3. P2：foreshadows 路线决策 + follow-up PR
4. P3：本机 `compileall` + `verify_entity_writeback_v19.sh` 验收

---

## 7. 更新日志

### 2026-05-01 — RUNBOOK 补齐 + P2 远端扫描
- 新增 `docs/RUNBOOK.md`（写入路径 / anti-pattern / 410 表 / 验收 / 灾难恢复 / PR 模板）
- `.gitignore` 新增产物目录排除：`.audit-*/`、`backups/`、`backend/.coverage`、`.coverage*`、`*.sse`
- 远端 GitHub `search_code` 扫描 11 条 PG 直写模式 = 0 命中
- 发现 P2 缺口：`foreshadows.py` 仍 PG 直写 → 列入 follow-up
- P0 仍需用户本地执行（远端代理无本地 shell）

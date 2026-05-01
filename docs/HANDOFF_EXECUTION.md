# Handoff (执行版) — Neo4j Truth Source 收尾与回归验证

> 目的：把"设定集（settings entities）Neo4j 真相源 + Postgres 投影"的改造收口做干净，让新窗口/新同学只看本文即可接手继续推进。

## 0. 当前结论（必须记住）

- **Neo4j 是真相源**：`world_rules / relationships / locations / character_states` 等设定集实体，所有写入必须先落 Neo4j。
- **Postgres 是 read model**：PG 只保存从 Neo4j materialize 出来的投影数据，用于读优化。
- **禁止 PG 直写**：避免 drift。
- 例外：`foreshadows` 仍 PG 直写（**三处**：api 层 2 处 + service 层 1 处），路线待决策（详见 §3.3 与 docs/PROGRESS.md）。
- ⚠ **README 描述的 `/neo4j-settings/*` 与 `/admin/entities/materialize` 端点不在 main**：仅在 `feature/v1.0-big-bang` 上有 `dc98363 feat(v1.9): add neo4j settings write API + materialize projection`。当前 main 唯一可用入口是 `POST /api/projects/{pid}/outlines/{oid}/extract-settings`，详见 RUNBOOK §1。

### 已完成并已合并的关键 PR

- PR #1：v1.9 主要收敛（outlines extract / world_rules ETL / relationships deletion sync 等）
- PR #2：README + ITERATION_PLAN 文档持续维护
- PR #3：禁用 legacy PG 直写接口（`/world-rules`、`/relationships` 的 POST/PUT/DELETE → **410**，引导 `extract-settings`；v1.10 计划后推 `/neo4j-settings/*`）
- PR #4 / PR #5：HANDOFF_EXECUTION + PROGRESS 模板
- PR #6（本 PR `chore/runbook-and-handoff-sync`）：补齐 `docs/RUNBOOK.md` + 同步 PROGRESS/HANDOFF + 补 `.gitignore` 产物目录 + **本机执行 P0 + P3 compileall** + **新增 `scripts/verify_entity_writeback_v19.sh`** + **修正「main vs README 入口」事实** + **本 PR 中同步修正 README + 410 message 指向 `extract-settings`**

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

### 1.2 安全判断本地未提交改动 / stash 内容

本 PR 的实际执行经验（可复用）：

```bash
# 1) 看本地 worktree 改动 vs 远端的差距
git diff HEAD origin/main -- <path>

# 2) 如本地 worktree diff 与远端 PR diff 完全一致（hash 范围相同），即"本地改了已经合并的内容" → 安全丢弃：
git checkout -- <path>

# 3) 看 stash 内容是否已被远端覆盖：
git stash show --stat stash@{0}
git stash show -p stash@{0}
git show origin/main:README.md | grep -A 20 '<stash 里追加的小节标题>'
# 如远端已含同等内容 → 安全 drop：
git stash drop stash@{0}
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
- 当前 main 实际写入口：`POST /api/projects/{project_id}/outlines/{outline_id}/extract-settings`（先 Neo4j → 调 `_materialize_entities_to_postgres` 投回 PG）
- 计划中的写入口（v1.10 待实现，任何分支都未包含）：`POST /api/projects/{pid}/neo4j-settings/*`、`POST /api/admin/entities/materialize`
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
grep -R --line-number -E "(\\bForeshadow\\(|db\\.add\\(.*Foreshadow|db\\.delete\\(foreshadow)" backend/app || true
```

本 PR 实跑结果（2026-05-01，AWS MCP shell）：
- 前 6 条：除 `backend/app/models/project.py:156 class WorldRule(Base)` / `:425 class Relationship(Base)` 类定义（误匹配，正常）外，**全部 0 命中**
- 第 7 条 Foreshadow：命中 3 处（详见 §3.3）

### 3.3 已知 P2 缺口（必须 follow-up）

仓库内 **3 处** PG 直写 Foreshadow（已通过本地 grep 确认）：

- `backend/app/api/foreshadows.py:111`（POST 创建）：`foreshadow = Foreshadow(...)` + `db.add(foreshadow)`
- `backend/app/api/foreshadows.py:179`（DELETE）：`await db.delete(foreshadow)`
- `backend/app/services/foreshadow_manager.py:84`（service `create` 方法）：`db.add(Foreshadow(...))`，被章节生成 / 大纲提取链路调用

处理策略二选一：
- **选项 A**：（依赖 v1.10 先引入 `/neo4j-settings/*` 路由族）新增 `/neo4j-settings/foreshadows` 写入口 + materialize 投影回 `foreshadows` 表；把现有 `/foreshadows` 写接口改 410；同步把 `foreshadow_manager.create` 改走 Neo4j 写入口。
- **选项 B**：在 RUNBOOK §3 显式声明 `foreshadows` 不入 Neo4j 真相源链路；加回归测试避免别处误用。

---

## 4. E2E 验收（P3）

### 4.1 Python 语法/编译检查

```bash
cd /root/ai-write
python3 -m compileall -q backend/app
```

本 PR 实跑结果（2026-05-01，AWS MCP shell）：`COMPILEALL_OK`（无 SyntaxError）。

### 4.2 运行对账脚本

```bash
cd /root/ai-write
PROJECT_ID=<your_project_id> CHAPTER_IDX=0 \
  bash scripts/verify_entity_writeback_v19.sh
```

验收：脚本输出 `OK: 设定集 writeback v1.9 对账通过`；Neo4j → materialize → PG 的数据一致。

> ✅ 脚本已在本 PR 入仓（`scripts/verify_entity_writeback_v19.sh`，151 行，`bash -n` SYNTAX_OK）。
> 仅当存在真实 `PROJECT_ID` + 启动了 `localhost:8000` 后端 + 有 PG/Neo4j 连接时才能跑出业务对账结果；否则只跑 legacy 410 烟测 + 路由探测段也能验证「写入口约束」。

---

## 5. 交付物要求（你每次 PR 必须满足）

- PR 描述必须 4 段式：Context / Change / Verification / Docs updated
- 避免提交：`.audit-*`、`backups/`、`.coverage`、临时 sse 文件等（已在 `.gitignore`）

---

## 6. 下一步建议顺序（直接照做）

1. ✅ P0：本机已 reset 到 `origin/main`、stash 已清空、产物无污染（本 PR）
2. ✅ P1：本 PR 已补齐 `docs/RUNBOOK.md` 并修正 main vs README 事实
3. P2：foreshadows 路线决策 + follow-up PR（同时改 api 层 + service 层）
4. ✅ P3 part 1：本机 `compileall` 通过（本 PR）
5. ✅ P3 part 2：`verify_entity_writeback_v19.sh` 入仓（本 PR）
6. P3 part 3：在带真实 PROJECT_ID 的环境跑对账脚本，把结果回填本 PR 或 follow-up PR
7. P4：决策是否把 `feature/v1.0-big-bang` 的 `neo4j_settings`+`admin_entities` 合并到 main

---

## 7. 更新日志

### 2026-05-01（晚 · 二轮） — 入仓 verify 脚本 + 修正 main vs README 事实
- 通过 AWS MCP shell 在 `/root/ai-write` 实际执行 + 写入：
  - 新增 `scripts/verify_entity_writeback_v19.sh`（151 行，`bash -n` SYNTAX_OK，`chmod +x` 已生效）
  - 发现并修正：`backend/app/api/neo4j_settings.py` 与 `backend/app/api/admin_entities.py` **不在 origin/main**（仅存在 `feature/v1.0-big-bang` 上 `dc98363` 起的 v1.9 链）
  - main 上唯一可用的设定集写入口：`POST /api/projects/{pid}/outlines/{oid}/extract-settings`（`backend/app/api/outlines.py:152` → 内部调 `backend/app/tasks/entity_tasks.py:47 _materialize_entities_to_postgres`）
  - 新增 P4：架构 vs 文档一致性决策项（合并 v1.9 链 / 修订 README 二选一）

### 2026-05-01（晚） — 实跑 P0 + P3 compileall + 补 service 层 P2 发现
- 通过 AWS MCP shell 在 `/root/ai-write` 本机实际执行：
  - P0 完成：丢弃本地未提交 `settings.py`（已确认与 origin/main PR #3 等价）+ drop stash@{0}（已确认是 PR #2 子集）+ ff main 到 `17fb371`，工作区 clean
  - P3 part 1 完成：`python3 -m compileall -q backend/app` → `COMPILEALL_OK`
- 本地 grep 复核 P2：除 `models/project.py` 类定义（正常）外，前 6 条模式 0 命中
- 新增 P2 发现：`backend/app/services/foreshadow_manager.py:84` 也直接 `db.add(Foreshadow(...))`，不止 api 层 → 已写进 RUNBOOK §3 + PROGRESS §3

### 2026-05-01（早） — RUNBOOK 补齐 + P2 远端扫描
- 新增 `docs/RUNBOOK.md`（写入路径 / anti-pattern / 410 表 / 验收 / 灾难恢复 / PR 模板）
- `.gitignore` 新增产物目录排除：`.audit-*/`、`backups/`、`backend/.coverage`、`.coverage*`、`*.sse`
- 远端 GitHub `search_code` 扫描 11 条 PG 直写模式 = 0 命中
- 发现 P2 缺口：`foreshadows.py` 仍 PG 直写 → 列入 follow-up

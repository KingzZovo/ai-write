# Handoff (执行版) — Neo4j Truth Source 收尾与回归验证

> 目的：把“设定集（settings entities）Neo4j 真相源 + Postgres 投影”的改造收口做干净，让新窗口/新同学只看本文即可接手继续推进。

## 0. 当前结论（必须记住）

- **Neo4j 是真相源**：`world_rules / relationships / locations / foreshadows` 等设定集实体，所有写入必须先落 Neo4j。
- **Postgres 是 read model**：PG 只保存从 Neo4j materialize 出来的投影数据，用于读优化。
- **禁止 PG 直写**：避免 drift。

### 已完成并已合并的关键 PR

- PR #1：v1.9 主要收敛（outlines extract / world_rules ETL / relationships deletion sync 等）
- PR #2：README + ITERATION_PLAN 文档持续维护
- PR #3：禁用 legacy PG 直写接口（`/world-rules`、`/relationships` 的 POST/PUT/DELETE → **410**，引导 `/neo4j-settings/*`）

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

查看：
```bash
git stash list
```

只恢复需要的文件（不要恢复产物目录）：
```bash
# 示例：只恢复某个 stash 里的单个文件
# git checkout stash@{0} -- README.md
# git checkout stash@{0} -- ITERATION_PLAN.md
```

确认无用后清理：
```bash
git stash drop stash@{0}
# 或清空所有 stash（确认无用再做）
# git stash clear
```

### 1.3 产物目录防误提交（可选但建议）

如果本地经常出现这些目录，建议补 `.gitignore`：
- `.audit-*/`
- `backups/`
- `backend/.coverage`
- `*.sse`

---

## 2. 必做收尾：RUNBOOK 补齐（P1）

> 目标：任何人不会再误用 PG 直写。

### 2.1 检查并补充 `docs/RUNBOOK.md`

必须包含：
- 唯一正确写入路径：**write Neo4j → materialize → PG**
- 常用写入口（至少写清两个）
  - `POST /api/projects/{project_id}/neo4j-settings/*`
  - `POST /api/projects/{project_id}/outlines/{outline_id}/extract-settings`
- 手动 materialize：`POST /api/admin/entities/materialize`
- Legacy 接口现状：
  - `/api/projects/{project_id}/world-rules`、`/relationships` 的写接口返回 **410**（为什么、替代方案）

实施：
```bash
# 改 docs/RUNBOOK.md
# 提交到新分支并开 PR
```

验收：Runbook 能回答“我要写 world_rules/relationships 应该调用哪个入口？”

---

## 3. 防回归：全仓扫描是否还有 PG 直写（P2）

> 目标：从代码层面把 drift 的可能性清零。

在 repo 根目录执行：

```bash
cd /root/ai-write

# 1) 直接 SQL 写入（粗暴但有效）
grep -R --line-number -E "INSERT INTO (world_rules|relationships|locations|foreshadows)" backend/app || true

grep -R --line-number -E "UPDATE (world_rules|relationships|locations|foreshadows)" backend/app || true

grep -R --line-number -E "DELETE FROM (world_rules|relationships|locations|foreshadows)" backend/app || true

# 2) ORM 写入点（按模型名扫）
grep -R --line-number -E "\bWorldRule\(" backend/app || true
grep -R --line-number -E "\bRelationship\(" backend/app || true

# 3) db.add(...) 里是否出现这些模型（更精确）
grep -R --line-number -E "db\.add\(.*(WorldRule|Relationship|Location|Foreshadow)" backend/app || true
```

处理策略：
- 如果发现 **写 PG**：
  - 迁移到 Neo4j 写入 + materialize；或
  - 明确禁用/返回错误（类似 PR #3），并更新文档。

验收：没有新的“写 PG 的入口”。

---

## 4. E2E 验收（P3）

### 4.1 Python 语法/编译检查

```bash
cd /root/ai-write
python -m compileall -q backend/app
```

### 4.2 运行对账脚本（若存在）

> 之前脚本可能需要用 bash 运行（避免执行权限问题）。

```bash
cd /root/ai-write

# 示例（按你自己的 project/章节参数调整）
PROJECT_ID=<your_project_id> CHAPTER_IDX=0 bash scripts/verify_entity_writeback_v19.sh
```

验收：脚本输出 OK；Neo4j → materialize → PG 的数据一致。

---

## 5. 交付物要求（你每次 PR 必须满足）

- PR 描述必须回答：
  - 变更点是什么
  - 为什么要这么改（如何避免 drift/回归）
  - 如何验证（给出可复制命令）
- 避免提交：`.audit-*`、`backups/`、`.coverage`、临时 sse 文件等

---

## 6. 下一步建议顺序（直接照做）

1. P0：本地 reset 到 `origin/main`、清 stash、清产物
2. P1：补 `docs/RUNBOOK.md`（最容易遗漏且影响最大）
3. P2：grep 全仓，清理残留 PG 直写
4. P3：compileall + verify 脚本验收

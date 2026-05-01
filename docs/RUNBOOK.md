# AI Write 运行手册（RUNBOOK）

> 本文档面向「运维 / 开发者 / 接手新窗口」。**写设定集只看这一份**就够。

## 0. 一句话原则

- **Neo4j = 真相源（source of truth）**：`world_rules / relationships / locations / character_states` 等设定集实体的所有写入必须先落 Neo4j。
- **Postgres = 读优化投影（read model）**：通过 materialize 从 Neo4j 投回 PG，读路径用 PG。
- **禁止 PG 直写**：避免 drift。
- 例外（待决策）：`foreshadows` 当前仍走 PG ORM 直写，详见 §3 与 docs/PROGRESS.md。

## 1. 正确写入路径（推荐入口）

### 1.1 通用 Neo4j 设定集写入接口

```
POST /api/projects/{project_id}/neo4j-settings/world-rules
POST /api/projects/{project_id}/neo4j-settings/relationships
POST /api/projects/{project_id}/neo4j-settings/locations
POST /api/projects/{project_id}/neo4j-settings/character-states
```

约束：先落 Neo4j；触发或随后调用 materialize 把投影同步回 PG。

### 1.2 从大纲提取设定集

```
POST /api/projects/{project_id}/outlines/{outline_id}/extract-settings
```

Extractor 会把 `characters / world_rules / relationships` 抽取并落到 Neo4j；不要绕过它去手写 PG。

### 1.3 手动 materialize（强制刷新 PG 投影）

```
POST /api/admin/entities/materialize
```

触发时机：
- 怀疑 PG 投影脏了 / drift
- Neo4j 直改后想立刻在前端看到
- 离线 ETL / 数据修复后

## 2. Anti-pattern（绝对禁止）

```python
# ❌ 直接 ORM 写 PG
db.add(WorldRule(...))
db.add(Relationship(...))
db.add(Location(...))
await db.delete(world_rule)

# ❌ 直接 SQL
await db.execute(insert(WorldRule).values(...))
await db.execute(text("INSERT INTO world_rules ..."))
```

任何对 `world_rules / relationships / locations / character_states` 的「写」都视为 drift 风险，应：
1. 改为先写 Neo4j，再 materialize；或
2. 标记为 410，引导到 `/neo4j-settings/*`。

## 3. Legacy 接口现状

| Path | Method | 现状 | 替代 |
|------|--------|------|------|
| `/api/projects/{project_id}/world-rules` | POST/PUT/DELETE | 410 Gone | `/neo4j-settings/world-rules` |
| `/api/projects/{project_id}/relationships` | POST/PUT/DELETE | 410 Gone | `/neo4j-settings/relationships` |
| `/api/projects/{project_id}/foreshadows` | POST/PUT/DELETE/resolve | ⚠ 当前仍 PG 直写（待路线决策） | TBD（详见 docs/PROGRESS.md P2） |

> ⚠ Foreshadows：仓库内目前有**两处** PG 直写 Foreshadow（已通过本地 grep 确认）：
> - `backend/app/api/foreshadows.py`：POST/PUT/DELETE/resolve 直接 `db.add(Foreshadow(...))` / `db.delete(foreshadow)` / `setattr(...)`
> - `backend/app/services/foreshadow_manager.py:84`（`create` 方法）：`db.add(Foreshadow(...))`，被章节生成 / 大纲提取链路调用
>
> 需要在下一个 follow-up PR 里二选一：
> - **选项 A**：纳入 Neo4j 真相源链路 —— 新增 `/neo4j-settings/foreshadows` 写入口 + materialize 投影；同时把 `foreshadow_manager.create` 改走 Neo4j 写入口。
> - **选项 B**：显式声明 `foreshadows` 不入 Neo4j 真相源链路（写在本节），并补回归测试防漂移。

## 4. 验收命令（复制即用）

### 4.1 本地状态干净（P0）

```bash
cd /root/ai-write

git fetch origin main
git reset --hard origin/main   # 仅在本地无价值脏改动时使用

git status            # 期望: working tree clean
git log -1 --oneline  # 期望: 与 origin/main HEAD 一致
```

> 如果本地有未提交改动且不确定能否丢弃：**先 `git diff` 与 `git diff HEAD origin/main -- <path>` 对比**。如本地改动恰好等于 origin 已合并 PR 的 diff，可安全 `git checkout -- <path>` 丢弃；否则用 `git stash -u` 暂存再 reset。

### 4.2 Stash 处理

```bash
git stash list
# 想看 stash 内容：
# git stash show --stat stash@{0}
# git stash show -p stash@{0}
# 仅按需要恢复单个 md：
# git checkout stash@{0} -- README.md
# 确认无用：
# git stash drop stash@{0}
# 或清空：
# git stash clear
```

### 4.3 Python 编译/语法（P3）

```bash
cd /root/ai-write
python3 -m compileall -q backend/app
```

期望：无输出（全部通过）或仅警告，无 SyntaxError。

### 4.4 设定集对账（P3，如脚本就位）

```bash
cd /root/ai-write
PROJECT_ID=<uuid> CHAPTER_IDX=0 \
  bash scripts/verify_entity_writeback_v19.sh
```

期望：脚本输出 OK，Neo4j 与 PG 投影一致。

> 当前 `scripts/` 目录在仓库中**不存在**（`verify_entity_writeback_v19.sh` 尚未入仓）。需要先把脚本提交到 `scripts/` 才能跑这一步；列入 P3 follow-up。

### 4.5 Legacy 410 烟测

```bash
# 应返回 HTTP 410
curl -i -X POST http://localhost:8000/api/projects/<pid>/world-rules
curl -i -X POST http://localhost:8000/api/projects/<pid>/relationships
```

### 4.6 残留 PG 直写本地 grep（P2）

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

期望：
- 前 6 条：除 `models/project.py` 的 `class WorldRule(Base)` / `class Relationship(Base)` 类定义（误匹配，正常）外，全部 0 命中。
- 第 7 条 Foreshadow：当前预期命中 `backend/app/api/foreshadows.py:111`、`:179` 与 `backend/app/services/foreshadow_manager.py:84`，待 follow-up PR 处理后清零。

## 5. 灾难恢复（PG 投影坏了 / drift）

1. 不要直接改 PG。
2. 找到 Neo4j 真相源；如有备份按部门规约恢复。
3. 调 `POST /api/admin/entities/materialize` 重建 PG 投影。
4. 如步骤 3 后仍不一致，手动 truncate PG 对应表（`world_rules / relationships / locations / character_states`）后再 materialize。
5. 复跑 §4.4 对账脚本确认（如脚本已就位）。

## 6. 产物目录禁止入仓

以下目录/文件**永远不进仓库**，统一在 `.gitignore` 中排除：

- `.audit-*/`
- `backups/`
- `backend/.coverage`、`.coverage`、`.coverage.*`
- `*.sse`
- `__pycache__/`、`*.py[cod]`、`.venv/`、`venv/`
- `node_modules/`、`.next/`、`out/`

如新窗口频繁误提交，先补 `.gitignore`，并在该 PR 描述里写清楚"为什么"。

## 7. 4 段式 PR 描述模板

每个可合并 PR 必须包含以下 4 段：

```
## Context
为什么改 / 修复了什么 drift / 哪条 P0–P3 项。

## Change
改了什么文件 / 增删了什么入口 / 新增 / 废弃 了什么。

## Verification
# 可复制命令 + 预期输出
cd /root/ai-write
python3 -m compileall -q backend/app
# expected: no SyntaxError

## Docs updated
- docs/RUNBOOK.md
- docs/PROGRESS.md
- docs/HANDOFF_TODO.md
- ...
```

如本次没改 docs/RUNBOOK，就在 Docs updated 写明"无需改"，并说明原因。

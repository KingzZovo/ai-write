# AI Write 运行手册（RUNBOOK）

> 本文档面向「运维 / 开发者 / 接手新窗口」。**写设定集只看这一份**就够。

## 0. 一句话原则

- **Neo4j = 真相源（source of truth）**：`world_rules / relationships / locations / character_states` 等设定集实体的所有写入必须先落 Neo4j。
- **Postgres = 读优化投影（read model）**：通过 materialize 从 Neo4j 投回 PG，读路径用 PG。
- **禁止 PG 直写**：避免 drift。
- 例外（待决策）：`foreshadows` 当前仍走 PG ORM 直写，详见 §3 与 docs/PROGRESS.md。

## 1. 正确写入路径（推荐入口）

### 1.1 当前 main 上的实际写入口（事实）

main HEAD（截至 `17fb371`）上 **唯一已实现** 的设定集写入口：

```
POST /api/projects/{project_id}/outlines/{outline_id}/extract-settings
```

该接口内部直接调用 Neo4j 写入 + 在最后一步调用 `_materialize_entities_to_postgres()` 把投影同步回 PG（见 `backend/app/api/outlines.py:152` 与 `backend/app/tasks/entity_tasks.py:47`）。章节生成 / 大纲提取链路也通过同一条路径触发 entity_tasks。

### 1.2 README 里写的「通用 Neo4j 设定集写入接口」（注意：未在 main 实现）

```
# README v1.9+ 文档化入口
POST /api/projects/{project_id}/neo4j-settings/world-rules
POST /api/projects/{project_id}/neo4j-settings/relationships
POST /api/projects/{project_id}/neo4j-settings/locations
POST /api/projects/{project_id}/neo4j-settings/character-states

# README v1.9+ 文档化手动 materialize
POST /api/admin/entities/materialize
```

> ⚠ **现状（已通过 `git ls-files backend/app/api/` 确认）**：`backend/app/api/neo4j_settings.py` 与 `backend/app/api/admin_entities.py` **不在 origin/main**（仅在 `feature/v1.0-big-bang` 历史里有过 `dc98363 feat(v1.9): add neo4j settings write API + materialize projection`）。
>
> README 里这些接口属于「目标架构」，不是当前 main 的可用入口。在它们落地之前，请使用 §1.1 的 `extract-settings` 入口。

### 1.3 从大纲提取设定集（main 实际可用）

```
POST /api/projects/{project_id}/outlines/{outline_id}/extract-settings
```

Extractor 会把 `characters / world_rules / relationships` 抽取并落到 Neo4j；不要绕过它去手写 PG。

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
2. 标记为 410，引导到 §1.1 的 `extract-settings`（或将来的 `/neo4j-settings/*`）。

## 3. Legacy 接口现状

| Path | Method | 现状 | 替代 |
|------|--------|------|------|
| `/api/projects/{project_id}/world-rules` | POST/PUT/DELETE | 410 Gone | `extract-settings`（§1.1） |
| `/api/projects/{project_id}/relationships` | POST/PUT/DELETE | 410 Gone | `extract-settings`（§1.1） |
| `/api/projects/{project_id}/foreshadows` | POST/PUT/DELETE/resolve | ⚠ 当前仍 PG 直写（待路线决策） | TBD（详见 docs/PROGRESS.md P2） |

> ⚠ Foreshadows：仓库内目前有**三处** PG 直写 Foreshadow（已通过本地 grep 确认）：
> - `backend/app/api/foreshadows.py:111`（POST 创建）
> - `backend/app/api/foreshadows.py:179`（DELETE）
> - `backend/app/services/foreshadow_manager.py:84`（service `create`，被章节生成 / 大纲提取链路调用）
>
> 需要在下一个 follow-up PR 里二选一：
> - **选项 A**：纳入 Neo4j 真相源链路 —— 新增 `/neo4j-settings/foreshadows` 写入口 + materialize 投影；同时把 `foreshadow_manager.create` 改走 Neo4j 写入口。
> - **选项 B**：显式声明 `foreshadows` 不入 Neo4j 真相源链路（写在本节），并补回归测试防漂移。

## 4. 验收命令（复制即用）

Python 包不装在宿主上。要打 patch 、跳试都从容器里跳：

```bash
docker exec -e PYTHONPATH=/app ai-write-backend-1 \
  bash -c 'cd /app && python -m pytest tests/ -q --ignore=tests/integration'
```

## 3. 热补丁（代码修改的完整路径）

**重要**：backend 与 celery-worker 是同一个镜像但是不同容器。你 host 上修改 `app/...` 后要 `cp` 两份、重启两份。

```bash
# 1) 宿主修改
vim /root/ai-write/backend/app/services/<file>.py

# 2) 拷到两个容器
docker cp /root/ai-write/backend/app/services/<file>.py \
  ai-write-backend-1:/app/app/services/<file>.py
docker cp /root/ai-write/backend/app/services/<file>.py \
  ai-write-celery-worker-1:/app/app/services/<file>.py

# 3) 重启两个容器
docker restart ai-write-backend-1 ai-write-celery-worker-1
sleep 8

# 4) 验证
curl -fsS http://127.0.0.1:8000/api/health
docker exec -e PYTHONPATH=/app ai-write-backend-1 \
  bash -c 'cd /app && python -m pytest tests/test_<变动面>.py -q'
```

反例：**切勿** 只修改宿主不 cp 进容器——那会造成 backend 看不到、但 pytest 里看到的“幽灵修复”。

## 4. 数据库 / 迁移

### 4.1 查看状态

```bash
docker exec ai-write-backend-1 alembic current
docker exec ai-write-backend-1 alembic history --verbose | head -60
```

当前 head：`a1001901`。

### 4.2 生成 / 应用迁移

```bash
# 生成新迁移（记得起名要能看出意图）
docker exec ai-write-backend-1 alembic revision --autogenerate -m 'v18_xxx'

# 人工调整 backend/alembic/versions/<new>.py 到满意
# 拷进两个容器
docker cp backend/alembic/versions/<new>.py ai-write-backend-1:/app/alembic/versions/
docker cp backend/alembic/versions/<new>.py ai-write-celery-worker-1:/app/alembic/versions/

# 应用
docker exec ai-write-backend-1 alembic upgrade head
```

回退：`alembic downgrade -1` 。产环境谨慎，先做备份。

### 4.3 备份 / 恢复

仓库提供了 `scripts/backup.sh`，输出到 `backups/`：

```bash
cd /root/ai-write && bash scripts/backup.sh
ls -la backups/                       # 看拿了哪些表 + qdrant snapshot
```

手动 PG 备份 / 恢复：

```bash
# 备份全库
docker exec ai-write-postgres-1 \
  pg_dump -U postgres -d aiwrite -Fc -f /tmp/aiwrite-$(date +%F).dump
docker cp ai-write-postgres-1:/tmp/aiwrite-$(date +%F).dump backups/

# 恢复
docker cp backups/aiwrite-2026-04-28.dump ai-write-postgres-1:/tmp/x.dump
docker exec ai-write-postgres-1 \
  pg_restore -U postgres -d aiwrite --clean --if-exists /tmp/x.dump
```

Qdrant 备份（snapshot REST）：

```bash
curl -fsS -X POST http://127.0.0.1:6333/collections/text_chunks/snapshots
ls /var/lib/docker/volumes/ai-write_qdrant_data/_data/snapshots/text_chunks/
```

## 5. 常用查询模板

## 6. 实体写回（Neo4j → Postgres）/ 观测与手动触发（v1.9+）

### 6.1 目的

- 生产链路里，实体抽取与写回发生在 Celery worker 进程；而 `/metrics` 是 FastAPI backend 进程提供。
- 因此，要在 `/metrics` 上看到 `entity_pg_materialize_total{...}` 的样本行，需要在 backend 进程内执行一次 materialize。

### 6.2 Admin 触发接口（backend 进程内执行）

接口：`POST /api/admin/entities/materialize`

鉴权：

- Header：`Authorization: Bearer <JWT>`
- 额外 gate：JWT 的 `sub` 必须在环境变量 `ADMIN_USERNAMES` 里（例如：`king`）

部署注意：

- `ADMIN_USERNAMES` 属于**部署配置**（运行时环境变量），不应依赖仓库内文件。
- 本仓库的 `.env` 文件被 `.gitignore` 忽略（不会被提交），因此**换机/重建环境时必须由部署侧提供该变量**。
- 推荐做法：
	- 临时运行：`ADMIN_USERNAMES=king docker compose up -d`
	- 长期运行：将 `ADMIN_USERNAMES` 写入你的服务管理/部署系统（例如 systemd、k8s Secret、私有 env 管理等）。

示例：

```bash
TOK=$(cat /tmp/king_tok)
curl -sS -X POST http://127.0.0.1:8000/api/admin/entities/materialize \
	-H "Content-Type: application/json" \
	-H "Authorization: Bearer $TOK" \
	-d '{"project_id":"<project_id>","chapter_idx":11,"caller":"runbook.manual"}'

# 预期：返回 status=ok + chars_seen/rels_seen 等
```

### 6.3 观测验证

```bash
curl -s http://127.0.0.1:8000/metrics | grep -E '^entity_pg_materialize_total\{' || true

# 预期至少出现 1 行类似：
# entity_pg_materialize_total{outcome="success",reason="ok"} 1
```

### 6.4 一键验收脚本（推荐）

仓库提供脚本：`scripts/verify_entity_writeback.sh`

```bash
PROJECT_ID=<project_id> bash scripts/verify_entity_writeback.sh
```

该脚本会依次执行：

- 调用 admin materialize 接口（backend 进程内）
- 查询 PG `characters/relationships` 计数与关键角色抽样
- grep `/metrics` 里是否出现 `entity_pg_materialize_total{...}`

### 6.5 Worker 主链路验收（Celery 执行）

说明：

- 生产链路中，`entities.extract_chapter` 由 Celery worker 执行。
- worker 侧执行 materialize **不会**在 backend 的 `/metrics` 上体现（`/metrics` 由 backend 进程暴露）。

手动 enqueue（从 backend 容器发任务到 worker）：

```bash
docker exec ai-write-backend-1 python -c "from app.tasks import celery_app; r=celery_app.send_task('entities.extract_chapter', kwargs={'project_id':'<project_id>','chapter_idx':11,'caller':'runbook.manual.enqueue'}); print(r.id)"
```

查看 worker 是否收到/执行：

```bash
docker logs --tail 200 ai-write-celery-worker-1 | grep -E 'Task entities\\.extract_chapter\\[|entity_extraction skip|Entity extraction complete' | tail -n 50
```

对账（以 Postgres 为准）：

```bash
docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -c "\
SELECT COUNT(*) AS characters_n FROM characters WHERE project_id='<project_id>';\
SELECT COUNT(*) AS relationships_n FROM relationships WHERE project_id='<project_id>';\
"
```

常见结果：

- 若该章节已完成抽取，会出现 `entity_extraction skip: already completed`，任务会以 `status=skipped` 结束（符合预期）。

### 6.6 rel_type 清洗（存量数据一次性修复）

背景：历史数据里 `relationships.rel_type` 可能包含括号解释或斜杠组合（例如 `对立/不信任（...）`），会影响筛选统计与下游校验。

仓库提供脚本（可重复执行）：`scripts/normalize_relationship_rel_type_pg.sh`

```bash
PROJECT_ID=<project_id> bash scripts/normalize_relationship_rel_type_pg.sh
```

### 6.7 rel_type 规范化（canonical 词表）

目标：让 `relationships.rel_type` 保持短、稳定、可枚举，以便：筛选统计、关系图展示、以及 OOC checker 关键字匹配。

当前 v1.9 的规范化策略：

- 写入 PG 的 outlines/extract 路径：按关键字归一到 canonical token（例如 敌对/对立/监管/审讯/师生/上下级/同舍/同伴/失联）。
- Neo4j → PG materialize 路径：同样按关键字归一。
- 存量 PG 数据：使用 `scripts/normalize_relationship_rel_type_pg.sh`，当 `rel_type` 过长时会把原值保存在 `label`（若 label 为空），并把 `rel_type` 归一化。

### 6.8 relationships 去重（存量数据一次性修复）

背景：当历史数据已存在重复关系，或 rel_type 规范化导致“同一对角色同一 rel_type”出现多条记录时，需要做一次性去重。

去重 key：`(project_id, source_id, target_id, rel_type)`（保留最早 created_at 的那条）。

仓库提供脚本（可重复执行）：`scripts/dedupe_relationships_pg.sh`

```bash
PROJECT_ID=<project_id> bash scripts/dedupe_relationships_pg.sh
```

### 5.1 PG 业务状态

```sql
-- 项目详情
SELECT id, name, status, created_at
FROM projects
ORDER BY created_at DESC LIMIT 10;

-- 某项目下的卷与章节（注意列名别名避免歧义）
SELECT v.idx AS volume_idx, v.title AS volume_title,
       c.chapter_idx, c.title AS chapter_title,
       c.word_count, c.status
FROM chapters c
JOIN volumes v ON v.id = c.volume_id
WHERE v.project_id = '<project_id>'
ORDER BY v.idx, c.chapter_idx;

-- 大纲分层（正确列是 level 不是 scope/outline_type）
SELECT level, COUNT(*) FROM outlines
WHERE project_id = '<project_id>' GROUP BY level;

-- 评分详情
SELECT chapter_id, plot_coherence, character_consistency, style_adherence,
       narrative_pacing, foreshadow_handling, overall,
       json_array_length(issues_json) AS n_issues
FROM chapter_evaluations
ORDER BY created_at DESC LIMIT 20;

-- cascade 任务分布
SELECT status, severity, COUNT(*) FROM cascade_tasks
GROUP BY status, severity ORDER BY status, severity;
```

### 5.2 LLM 调用日志

```sql
-- 依据会必须走 task_type / model_name，**llm_call_logs 表无 provider 列**
SELECT task_type, model_name, COUNT(*) AS n,
       AVG(duration_ms) AS avg_ms
FROM llm_call_logs
WHERE created_at > now() - interval '1 day'
GROUP BY task_type, model_name ORDER BY n DESC;

-- 错误率
SELECT model_name,
       SUM(CASE WHEN status='error' THEN 1 ELSE 0 END)::float / COUNT(*) AS err_rate,
       COUNT(*) AS n
FROM llm_call_logs
WHERE created_at > now() - interval '1 day'
GROUP BY model_name;
```

### 5.3 Prometheus PromQL

```promql
# 其他业务调用量 TPS
rate(llm_call_total[5m])

# 错误率（按 task_type）
sum by (task_type)(rate(llm_call_total{status="error"}[5m]))
  /
sum by (task_type)(rate(llm_call_total[5m]))

# v1.7.2 后 provider 变为类名，指名中包含 Provider/Proxy
sum by (provider)(
  rate(llm_call_total{provider=~".*Proxy|.*Provider"}[5m])
)

# token 使用量
sum by (direction)(rate(llm_token_total[5m]))

# 生成调用 P95 耗时
histogram_quantile(0.95,
  sum by (le)(rate(llm_call_duration_seconds_bucket{task_type="generation"}[5m]))
)

# scene plan fallback
sum by (reason)(increase(scene_plan_fallback_total[1h]))
```

## 6. 故障诊断（按现象倒查）

### 6.1 `/api/health` 不返 200

1. `docker logs --tail 200 ai-write-backend-1` 看启动 traceback。
2. 最常见原因：alembic 未升、`DATABASE_URL` 接不上、运行时导入失败。
3. 跳进容器手走一遍 `python -c 'from app.main import app'` 能马上看出哪个模块出事。

### 6.2 celery-worker 刷 `loop` warning / 任务不动

1. `docker logs --tail 200 ai-write-celery-worker-1`。
2. 不同名字任务不互部，看是否某个 `tasks.<name>` 未被 register（`celery -A app.celery_app inspect registered`）。
3. Redis 是否还在：`docker exec ai-write-redis-1 redis-cli ping` 期望 `PONG`。
4. 用 `celery inspect active / inspect reserved` 看队列。

### 6.3 cascade 任务卡在 `pending`

1. 检查同一 `(source_chapter_id, target_entity_type, target_entity_id, severity)` 老任务 —— UNIQUE 索引会拒受重复跟进。
2. 看对应评分 `chapter_evaluations.issues_json` 中 high/critical 个数是否 ≥ 3 且存在 `IN_SCOPE_DIMENSIONS`。
3. `LOCK_RETRY_COUNTDOWN=30` 仅处理锁失败；如果调度未起，看 worker 是否被 `worker_concurrency` 占满。

### 6.4 LLM 调用全部 `error`

1. `curl -fsS http://141.148.185.96:8317/v1/models` 测上游是否还活。
2. 看 `llm_call_logs` 最近几条的 `error_text`，401 = token 过期；502/timeout = 上游；validation = task_routing 打错了。
3. v1.7.1 Z1 后，所有 task_type 都能在 `llm_call_total` 上看到；发现 “unknown” task_type 突增 → 某个入口忘了传。

### 6.5 PG 查询报 `column ... ambiguous`

- chapters 与 volumes 都有 `title`，JOIN 要 `c.title AS chapter_title` / `v.title AS volume_title`。
- outlines 上是 `level` 不是 `scope`/`outline_type`。
- 仓库里不存在 `task_routing` 表，路由配置从 `prompt_assets` + 代码常量拼出。如需调路由，去 `app/services/model_router.py`。

### 6.6 Qdrant 丢 collection / 数量对不上

```bash
curl -s http://127.0.0.1:6333/collections | jq
curl -s http://127.0.0.1:6333/collections/text_chunks | jq
```

期望：`text_chunks=2159, style_samples_redacted=4113, beat_sheets=4115, style_profiles=4115`。如果丢了，走 `tasks.vectorize_book` 重跑，但要先看 `reference_book_slices` 在不在。孤儿片清理 → `scripts/cleanup_orphan_qdrant_slices.py`。

### 6.7 frontend 接不上后端

1. 在浏览器 devtool 看请求是去了 `:8080`/`:3100` 还是裸 8000。
2. nginx 配置：`nginx/`。`docker logs ai-write-nginx-1`。
3. tsc 原地体检：`cd /root/ai-write/frontend && npx --no-install tsc --noEmit`。空输出 = OK。

## 7. 发布流程（入主干 + tag）

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

### 4.4 设定集对账（P3）

```bash
cd /root/ai-write
PROJECT_ID=<uuid> CHAPTER_IDX=0 \
  bash scripts/verify_entity_writeback_v19.sh
```

期望：脚本输出 `OK: 设定集 writeback v1.9 对账通过`，Neo4j 与 PG 投影一致。

> 脚本已在本 PR 入仓（`scripts/verify_entity_writeback_v19.sh`，151 行，bash -n SYNTAX_OK）。
> 依赖：`curl`、`psql`；可选 `cypher-shell`（无则跳过 Neo4j 端对账，仅做 API + PG 端）。

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
3. **当前 main 还没有暴露 `POST /api/admin/entities/materialize` 公开端点**。临时方案：通过任意章节调用 `extract-settings`（§1.1）会触发同一个 `_materialize_entities_to_postgres()`；或临时 import `backend/app/tasks/entity_tasks._materialize_entities_to_postgres` 在 shell 里直接调用。
4. 如步骤 3 后仍不一致，手动 truncate PG 对应表（`world_rules / relationships / locations / character_states`）后再触发 §1.1。
5. 复跑 §4.4 对账脚本确认。

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

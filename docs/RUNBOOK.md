# ai-write 运维手册（RUNBOOK）

> **适用版本**：`v1.7.2`，仓库 `/root/ai-write`。
> **对象**：本机运维 + 代理 (本人+代码agent)。
> **适配拓扑**：单机 docker-compose，服务全部绑 `127.0.0.1` (除 nginx 8080 / frontend 3100 / grafana 3001)。
>
> 架构背景请读 `ARCHITECTURE.md`；本文只处理 “怎么运起来 / 怎么修、怎么查” 三件事。

---

## 0. 账号与创a勿氪仪

| 项 | 值 |
|---|---|
| 项目路径 | `/root/ai-write` |
| 分支 | `feature/v1.0-big-bang`（当前主干）|
| 最后 tag | `v1.7.2 → 31c0362` |
| Postgres 库/用户 | `aiwrite` / `postgres` |
| Backend HTTP | `127.0.0.1:8000` |
| Frontend | `http://<host>:3100` (nginx 反代) |
| Smoke 账号 | `king` / `Wt991125` |
| 本地 token 文件 | `/tmp/king_tok` |
| LLM 上游 | `141.148.185.96:8317` |
| Grafana | `http://<host>:3001` |
| Prometheus | `http://<host>:9091` |

## 1. 快速健康检查（全生产最初 30 秒）

```bash
# 1) 容器状态
docker ps --format '.Names\t.Status' | grep ai-write

# 2) 后端健康
curl -fsS http://127.0.0.1:8000/api/health

# 3) Prometheus 拉取
curl -fsS http://127.0.0.1:9091/-/ready

# 4) 看 worker 近 50 行日志是否有 ERROR
docker logs --tail 50 ai-write-celery-worker-1 | grep -E 'ERROR|Traceback' || echo 'worker clean'
```

预期：10 个容器都 Up；`/api/health` 返 200；`prometheus /-/ready` 返 `Prometheus Server is Ready.`。

## 2. 常用运维动作

### 2.1 启 / 停 / 重启

```bash
cd /root/ai-write
docker compose up -d                       # 启动全部
docker compose ps                          # 看状态
docker compose down                        # 优雅停 (保留 volume)
docker compose down -v                     # 连同卷一起干净。危险！

docker restart ai-write-backend-1 ai-write-celery-worker-1 && sleep 8
curl -fsS http://127.0.0.1:8000/api/health
```

### 2.2 看日志

```bash
docker logs --tail 200 -f ai-write-backend-1
docker logs --tail 200 -f ai-write-celery-worker-1
docker logs --tail 100 ai-write-postgres-1
docker logs --tail 100 ai-write-qdrant-1
```

### 2.3 进容器 / shell

```bash
docker exec -it ai-write-backend-1 bash
docker exec -it ai-write-postgres-1 psql -U postgres -d aiwrite
docker exec -it ai-write-redis-1 redis-cli
```

### 2.4 跨容器跳验证虚拟环境

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

### 6.9 relationships 唯一约束（防止重复写入）

为保证写回幂等与防止重复数据，v1.9 增加 DB 级唯一约束：

- `relationships`：`(project_id, source_id, target_id, rel_type)`

升级前需要先执行一次去重脚本（见 6.8），否则迁移会失败。

### 6.10 唯一约束下的并发写入容错（IntegrityError 处理）

在 `relationships` 增加唯一约束后，极少数情况下（并发写入 / 重试竞态）可能触发 DB 的唯一性冲突。

v1.9 处理策略：

- 写入侧将唯一冲突视为“已存在”，进行 rollback 并继续，不影响主链路执行。
- 典型现象：entity materialize 日志里 `rels_created=0`，但整体 `status=ok`（符合预期）。

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

# 1) 确认工作区干净
git status

# 2) 跑测试 + 打中健康
docker exec -e PYTHONPATH=/app ai-write-backend-1 \
  bash -c 'cd /app && python -m pytest tests/ -q --ignore=tests/integration'
curl -fsS http://127.0.0.1:8000/api/health

# 3) 提交
git -c user.name=agent -c user.email=agent@local \
  commit -m 'feat(...): ...'

# 4) 写 RELEASE_NOTES_vX.Y.Z.md + 补 CHANGELOG 顶部
# 5) 底部 commit 完成后打 tag
git tag -a vX.Y.Z -m 'release notes summary'

# 6) 推（如需）
git push && git push --tags
```

## 8. 上手检查清单（新人/代理首次上机）

- [ ] `docker compose ps` 10 个服务 Up
- [ ] `/api/health=200`、`/metrics` 能拉到带 `llm_call_total` 的 sample
- [ ] `alembic current` = `a1001900`
- [ ] `pytest -q` 252 passed
- [ ] `docker exec ai-write-postgres-1 psql -U postgres -d aiwrite -c '\dt'` 能看到 `chapters / volumes / outlines / cascade_tasks / chapter_evaluations / llm_call_logs / generation_runs`
- [ ] Qdrant collections 全在
- [ ] Prometheus 面板 “AI Write Overview”（grafana）能拉出带 status 标签的序列

## 9. 点名坑与约定

1. **两容器同步手动 cp**：什么时候纲领设仪改为热重载 / 重新 build 换镜像要在 v1.8 话题内决。现阶段严格遵守 §3 。
2. **`llm_call_logs` 表无 `provider` 列**：详见 §6.5。要看 provider 去 Prom。
3. **`outlines` 用 `level` 不是 `scope`/`outline_type`**。
4. **chapter_evaluator 与 cascade 门限常量**是业务交换面。修改请同步更新 ARCHITECTURE §5.2。
5. **scene_orchestrator 体量**存在边缘 case（ch3 过短 / ch4 过长）；调参后务必手跑一轮多章节生成验证。
6. **`time_llm_call` 包裹位置**：v1.7.2 Z3 仅包裹了 4 个同步主路。`stream_by_route` 与 `stream_with_tier_fallback` 在表层未包，下一轮补齐。
7. **提交人**：代理提交用 `agent <agent@local>`，人提交使用本人身份。勿混。

## 10. 参考文档

- `docs/ARCHITECTURE.md` — 什么是什么。
- `docs/v1.5.x-v1.6.0-roadmap.md` `docs/v1.4.x-v1.5.0-roadmap.md` — 路线图
- `RELEASE_NOTES_v1.7.{0,1,2}.md` — 变更记录
- `CHANGELOG.md` — 一眼画发布史
- `AGENTS.md` — 代理交接与作业约定

---

*遇到本手册未写、或写错、或已不适用的场景，请同仓提 PR 或直接 commit 修补。*

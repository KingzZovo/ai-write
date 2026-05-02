# ai-write 仓库代理须知

面向在本仓库内动手的 AI 代理 / 工程师。范围：runtime / backend / DB 约束、踩过的坑、跨章节生成链路的死锁防线。
不重复 Notion 全局规则页里已有的本地代理通用守则（MCP timeout、shell escape、conventional commit 等）。

---

## 🛡️ prompt_registry 死锁双层防线（v1.5.0 / C2 + C3）

**症状**：scene_mode + auto_revise 下，baseline 完成后 revise scene_writer 的 `UPDATE prompt_assets SET success_count` 无限期 hang；`pg_stat_activity` 看到一个 `idle in transaction` session 持 `SELECT prompt_assets...`、`wait_event=ClientRead`、年龄持续增长；revise UPDATE 被 `transactionid Lock` 阻塞。

**根因**：FastAPI `Depends(get_db)` 注入的 `AsyncSession` 在 baseline 阶段第一次 SELECT 触发自动 tx，整段 baseline 写入 + evaluate 期间不 commit/rollback，仍持 `prompt_assets` 行锁；进入 revise loop 时另一 session 的 UPDATE 在同一行上排队。

### Layer 1 — C2 fix（commit `e70222f`）
进入 revise loop 前 `await db.rollback()`（try-wrapped），手动结束 baseline 路径上的 idle-in-tx，释放行锁。

### Layer 2 — C3 fix（commit `289a121`）
- `prompt_registry` **read 路径** 改走 `prompt_cache.get_snapshot(task_type, db)`：进程级 TTL=300s 缓存 RouteSpec + tier；NEG_TTL=30s 防丢 task 反复查表。
- **write 路径** `track_result` 替换为 `prompt_cache.buffer_track_result(asset_id, success)`，仅内存累加，**不**触 DB。
- 后台 `flush_pending_counts()` 每 30s 用 `async_session_factory()` 开**独立 session** 把内存计数刷入 DB，与 request session 完全隔离。
- `app/api/prompts.py` 的 CRUD 后调用 `prompt_cache.invalidate(task_type)` 刷新；全量种子后用 `invalidate()` 清空。
- `app/main.py` lifespan：种子完成后 `start_flusher()`；shutdown 时 `stop_flusher()` 必须在 `engine.dispose()` 之前 drain。

### 效果
每次 LLM 调用从 `2 SELECT + 1 UPDATE` 变为 `0 SELECT (warm cache) + 0 UPDATE (deferred flush)`。request session 完全不再写 `prompt_assets`，**即使 layer 1 失守也不会复现死锁**。

### 新代码契约（强制）
Hot path（`run_text_prompt` / `stream_text_prompt` / SceneOrchestrator / 任意 SSE 内调用）**必须**用：
- `await prompt_cache.get_snapshot(task_type, db)` 取路由
- `await prompt_cache.buffer_track_result(asset_id, success)` 记成功/失败

**不要**走 `PromptRegistry.{get,resolve_route,resolve_tier,track_result}` 旧 API。这套旧 API 只为 admin CRUD 保留向后兼容。

### 配套修补（C2 同 commit `e70222f`）
1. `SceneOrchestrator` 必须把 `user_instruction` 透传到 `plan_scenes` 和 `scene_writer`。否则用户指令在 scene_mode 下静默丢失。
2. revise rewrite 必须用 `asyncio.timeout(900)` 包；超时则发 `revise_error` SSE，避免 LLM 卡死把整条 SSE 流挂死。

### 诊断 / 监控 SQL
找 idle-in-tx + 锁等待：
```sql
SELECT pid, state, wait_event_type, wait_event,
       age(now(), query_start) AS age,
       substring(query, 1, 100)
FROM pg_stat_activity
WHERE state != 'idle' AND backend_type = 'client backend'
ORDER BY query_start NULLS LAST;
```

request 期间监控 `prompt_assets` UPDATE 必须为 0：
```sql
SELECT count(*) FROM pg_stat_activity
 WHERE query ILIKE 'UPDATE prompt_assets%' AND state = 'active';
```

应急 kill 长 idle-in-tx：
```sql
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE state IN ('idle in transaction','active')
   AND backend_type = 'client backend'
   AND age(now(), query_start) > interval '60 seconds';
```

### 回归基线（post-C3）
- 全量 `pytest tests/ --ignore=tests/test_api_core.py` 应得 **`157 passed + 1 预存 flake`**（`test_v10_observability::test_metrics_endpoint_public`）。
- `tests/test_c3_prompt_cache.py` 14 个用例必须全过。
- Smoke #6 closure：chapter 3 `scene_mode + auto_revise threshold=9.0 max_rounds=1`，约 10 分钟到达 `[DONE]`，2 个新 `chapter_evaluations` 行（baseline 8.10 → revise 8.14, `rounds_exhausted=true`），全程 `pg_stat_activity` 探测 `UPDATE prompt_assets` = 0。

---

## 🗄️ DB / Schema 约束（必查清单）

- DB 名 **`aiwrite`**（无下划线）。psql 入口：`docker exec ai-write-postgres-1 psql -U postgres -d aiwrite`。
- `chapters`
	- 正文字段名是 **`content_text`**（不是 `text` / `content`）。写错列名 psql 直接 `column "text" of relation "chapters" does not exist`。
	- 章节序号字段是 **`chapter_idx`**（不是 `chapter_number`）。
- `chapter_evaluations`
	- **没有** `round` / `round_number` 列；通过 `created_at` 排序辨识 baseline vs revise。
	- `issues_json` 列类型是 `json` 不是 `jsonb`，调用 `jsonb_array_length(...)` 之前必须 `::jsonb` 强转。
	- 列：`plot_coherence`、`character_consistency`、`style_adherence`、`narrative_pacing`、`foreshadow_handling`、`overall`。
- `llm_endpoints`：默认模型字段是 `default_model`（不是 `model`），含 `tier` 列。
- `prompt_assets` 种子必须包含 `endpoint_id`，否则 lifespan 启动失败。
- `pg_stat_statements` 扩展未安装；`pg_stat_statements_reset()` 不可用。改用 `SUM(prompt_assets.success_count)` delta + `pg_stat_activity` 直接探测替代。
- Alembic 当前 head：`a1001700`；alembic 表结构与 `Base.metadata.create_all` 重复时直接 `DROP CASCADE` 再重建，不要试图手动 reconcile。

---

## 🌐 Runtime 拓扑

- 后端容器 `ai-write-backend-1` 实际暴露端口：**`127.0.0.1:8000`**。
- nginx 容器 `80→8080` 当前**不代理 `/api`**（返回 502）。smoke / curl 直接打 `http://localhost:8000/api/...`。
- 健康检查：`/api/health`（不是 `/health` / `/healthz`）。
- 章节生成：`POST /api/generate/chapter`（**没有** `/stream` 后缀）。SSE 流，关键事件：`status=generating` → 多个 `text` chunks → `status=saved {word_count}` → `event=evaluating` → `event=scored {round, overall, issues}` → 若不达标 `event=revising` → 多个 `text {revise_round}` → `status=saved {word_count, revise_round}` → 第二次 `scored {rounds_exhausted}` → `status=completed` → `[DONE]`。
- 鉴权：`POST /api/auth/login` 返回 JSON，token 字段名是 **`token`**（不是 `access_token`）。
- `/tmp/.tok` 缓存文件存完整登录 JSON `{"token":"..."}`，**不存**裸 JWT。读取：
	```bash
	TOKEN=$(python3 -c "import json;print(json.load(open('/tmp/.tok'))['token'])")
	```
- Neo4j：`neo4j/changeme123` @ `bolt://neo4j:7687`。
- 后端容器内跑 python 脚本必须 `docker exec -e PYTHONPATH=/app -w /app ai-write-backend-1 ...`，否则 `ModuleNotFoundError: No module named 'app'`。site-packages 不能与 `/app/app/` 并存同名包；发现 `/usr/local/lib/python3.11/site-packages/app/` 立即在 backend + celery 都 `pip uninstall ai-write-backend`。
- LLM endpoint：`141.148.185.96:8317`。

---

## 🧪 长任务 SSE smoke 范式

主控线程 timeout ≤ 30s，无法在前台 `curl --max-time 600` 等长跑。规范：

```bash
TOKEN=$(python3 -c "import json;print(json.load(open('/tmp/.tok'))['token'])")
nohup curl -sN \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -X POST http://localhost:8000/api/generate/chapter \
  -d '{"project_id":"...","chapter_id":"...","use_scene_mode":true,"auto_revise":true,"revise_threshold":9.0,"max_revise_rounds":1,"target_words":2500}' \
  >/tmp/c3-smoke.log 2>&1 &
echo PID=$!
```

然后用 `sleep 25 && grep -c '^data:' /tmp/c3-smoke.log && grep -oE '"status": "[^"]+"|"event": "[^"]+"' /tmp/c3-smoke.log | sort | uniq -c` 轮询事件分布。

**期间**每隔一两轮跑一次 `pg_stat_activity` 监控 SQL（见上面），确认 `UPDATE prompt_assets%` count = 0。看到任意非零都是回归。

**`[DONE]` 之后**立即查 `chapters` 与 `chapter_evaluations`：
```sql
SELECT id, status, word_count, length(content_text) AS len, updated_at
FROM chapters WHERE id = '<chapter_id>';

SELECT overall, plot_coherence, character_consistency,
       jsonb_array_length(issues_json::jsonb) AS issues, created_at
FROM chapter_evaluations
WHERE chapter_id = '<chapter_id>'
ORDER BY created_at DESC LIMIT 5;
```

---

## 📚 文件路线图（v1.5.0 hot-path 入口）

- `app/services/prompt_cache.py` — 进程级 prompt cache + buffered counter flusher（C3 核心）。公共面：`get_snapshot`、`invalidate`、`buffer_track_result`、`pending_counts`、`flush_pending_counts`、`start_flusher`、`stop_flusher`、`stats`、`reset_for_tests`。常量：`TTL_SECONDS=300`、`NEG_TTL_SECONDS=30`、`FLUSH_INTERVAL_SECONDS=30`。
- `app/services/prompt_registry.py` — `RouteSpec` 数据类、`BUILTIN_PROMPTS`、`_TASK_TYPE_FALLBACK`、`_resolve_route_and_tier_cached`（C3 注入点）、`run_text_prompt` / `stream_text_prompt` 走 cache。`PromptRegistry` 类保留只做 admin CRUD 后端。
- `app/api/prompts.py` — admin CRUD；每条 mutation 后 `prompt_cache.invalidate(task_type)`；全量 seed 后 `invalidate()`。
- `app/main.py` — lifespan 在 seed_builtins 后启动 flusher；shutdown 在 `engine.dispose()` 前 drain。
- `app/services/scene_orchestrator.py` — auto_revise loop 入口。进入 revise 前必须 `await db.rollback()`；`user_instruction` 必须透传到 `plan_scenes` + `scene_writer`；revise rewrite 必须 `asyncio.timeout(900)` + `revise_error` SSE 兜底。
- `app/api/generate.py` — `POST /api/generate/chapter` SSE 入口，`use_scene_mode + auto_revise + revise_threshold + max_revise_rounds + target_words` 字段定义在 `GenerateChapterRequest`。
- `tests/test_c3_prompt_cache.py` — 14 用例覆盖 cache 命中/失效/并发、buffered counter 离线 session、flush 失败重排队、resolver 异常路径。

---

## 📦 提交 / 验收例行

1. 改动后 AST 自检：`python3 -c 'import ast; ast.parse(open("<file>").read())'`。
2. `docker restart ai-write-backend-1` → 等 `Application startup complete.` → 检查 `Prompt cache flusher started` 出现。
3. 跑相关单测；最后跑全量 `pytest tests/ --ignore=tests/test_api_core.py -q`，断言基线 `157 passed + 1 预存 flake`。
4. 长流程改动跑一次 SSE smoke + `pg_stat_activity` 实时探测，记录 6 点验收：
	- `[DONE]` 是否到达
	- baseline 是否落库（`status='saved'`，`word_count > 0`）
	- revise 是否落库（带 `revise_round`）
	- 第二次 `event=scored` 是否带 `rounds_exhausted` / `triggered_revise`
	- `chapter_evaluations` 新增行数与得分
	- 全程 `UPDATE prompt_assets%` 探测计数 = 0
5. Conventional commit：`feat(<chunk>)`、`fix(<chunk>)`、`test(<chunk>)`、`docs(<scope>)`，正文写明合同 + smoke 证据。

---

## 🔧 当前已落地链（v1.5.0）

| chunk | 内容 | HEAD ↔ commit |
|---|---|---|
| C1 | scene_mode 写作链与一致性补丁 | `e16b182` ← `301b835` ← `b316d42` ← `729fc3f` ← `453b9ae` |
| C2 | auto_revise 闭环 + 死锁 layer 1 | `e70222f` ← `14d892e` ← `0f9ddd6` ← `ed75630` ← `2e31703` ← `4f6e9e0` ← `faa28b3` |
| C3 | prompt cache + buffered counter（layer 2） | `289a121` |
| C4 | cascade auto-regenerate | _planned_ |

---

## 🚧 下一步队列

- **C4**：cascade auto-regenerate。当 `event=scored` 带 `rounds_exhausted=true && overall < threshold` 且 `issues_json` 含跨章节 / 角色一致性级别问题时，把上游 chapter / outline / 角色卡 / world rules 入队列重生成。串行 + 同 project 限流 + 幂等。schema：`cascade_tasks(id, source_chapter_id, target_entity_type, target_entity_id, severity, status, parent_task_id, created_at, completed_at)`。

本文件随每个 chunk 完成同步更新，**不要**留过期信息。


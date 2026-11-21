# Release Notes — v1.5.0 (Scene-Staged Writing + Auto-Revise + Cascade)

**发布日期**：2026-04-27 (Asia/Shanghai)  
**基准**：v1.4.1  
**数据库头**：`a1001900`（新增 7 个迁移：a1001500 / a1001501 / a1001600 / a1001601 / a1001700 / a1001800 / a1001900）  
**Git tag**：`v1.5.0`  
**HEAD**：`bd3d9a6` (`docs(v1.5.0): acceptance report`)  
**验收报告**：`docs/v1.5.0-acceptance-report.md`

## TL;DR

v1.5.0 是 v1.4 tier routing 上面的**生成能力升级**，四大主线特性：

1. **C1 Scene-staged writing** — 章节不再单镜头，`scene_planner` (standard tier) 先出 N 个 200 字 brief，`scene_writer` (flagship tier) 逐场景写 800-1200 字流式输出。场景数 3..6 自动夹。
2. **C2 Auto-revise loop** — 写完 → evaluator 评分 → < `revise_threshold` 时把 issue 喂回 SceneOrchestrator 重写（默认 N=2 轮）；各轮 LLM 调用被 `asyncio.timeout(900)` 包裹防卡死。
3. **C3 Process-local prompt cache** — hot path 不再走 `prompt_assets` 表：TTL=300s 进程级 snapshot 缓存 + 30s 后台 flusher，request session 零写 `prompt_assets`，从根上溈灭 scene_mode + auto-revise 的死锁。
4. **C4 Cascade auto-regenerate** — evaluator critical issue → `cascade_planner` 生成上游任务 → `cascade_tasks` 表 + Celery worker → 章/纲/角/世界规则四类 handler in-place 幂等 revision。

配套：微调 evaluator/checker 的 tier-aware fallback (B1′)、Character/HAS_STATE writer 统一 (B2′)、prompt save 软警卫 (B2)、chapter SSE auto-save (Bug K 修复)。附带 5 条 pre-existing flake 全部清送。

---

## 1. C1 — Scene-staged writing

### 用户可见补丁

- `POST /api/generate/chapter` 请求体新增两个可选字段：
  - `use_scene_mode: bool = False`——默认 false 以保证向后兼容，为 true 时走场景阶段化路径。
  - `target_scenes: int | null` — 场景数提示，`SceneOrchestrator` 会夹到 [3, 6]。`null` = 自动。
- SSE 事件多出 `scene_brief_planned` / `scene_started` / `scene_completed` 类型，可用于前端进度动画。

### 后端实现

- `app/services/scene_orchestrator.py` (~302 行)：`SceneBrief` dataclass + `_fallback_scene_briefs()` (parse 失败/过短时按 `target_words` 均分产生默认 brief)。
- `prompt_assets` 新增两行（alembic `a1001600_v150_scene_writing` + `a1001601_v150_scene_endpoint_backfill`）：
  - `scene_planner` — standard tier / max_tokens=4096 / output_schema = `{scenes: [{title, summary, target_words}]}`
  - `scene_writer` — flagship tier / max_tokens=8192 / streaming
- `app/services/prompt_recommendations.py` 注册两个 task_type 推荐 (chat / standard, chat / flagship)。
- `app/api/generate.py:181-189` 启动分支；`:267-373` revise loop 与 C2 共住。

### 测试

`tests/test_c1_scene_orchestrator.py` 22 个回归用例（commit `301b835`）：fallback 边界、JSON parse 宽容、target_words 夹 、`user_instruction` 透传、SSE 事件顺序。

---

## 2. C2 — Auto-revise closed loop

### 请求字段

- `revise_threshold: float = 7.0` — evaluator overall < 阈值 → 走 revise。
- `revise_max_rounds: int = 2` — 默认最多 2 轮。

### 底层

- 新表 `evaluate_tasks`（alembic `a1001700_v150_evaluate_tasks`）+ ORM，支持 `POST /api/evaluate/start` + `GET /api/evaluate/tasks/{id}` 异步路径。
- 新 Celery task `evaluations.evaluate_chapter`（commit `2e31703`）。
- 辅助 helper `auto_revise_threshold` + `issues_to_revise_instruction`（commit `4f6e9e0`）。
- `api/generate.py:267-373` revise 闭环：进入前 `await db.rollback()` 释放 baseline 路径上 `prompt_assets` 行锁（AGENTS.md「Layer 1 fix」）；SceneOrchestrator 重代入 issue summary + `user_instruction`；每轮 LLM 调用包 `asyncio.timeout(900)`。

### 测试

`tests/test_c2_auto_revise.py` 24 个用例（commit `14d892e`）：evaluator helpers、EvaluateTask CRUD、dispatch、超时 SSE error 事件、rounds_exhausted 触发 cascade。

---

## 3. C3 — Prompt cache (deadlock prevention)

### 背景

C2 上线后发现 scene_mode + auto-revise 出现同一个 `prompt_assets` 行上的 deadlock：baseline 路径的 FastAPI `Depends(get_db)` session 在 SELECT 进入隐式 tx 后，整个 baseline + evaluate 期间持锁；进入 revise loop 另一 session 的 `UPDATE prompt_assets SET success_count` hang。

### 两层防线

- **Layer 1** (commit `e70222f`)：revise 前 `await db.rollback()`。
- **Layer 2** (commit `289a121`)：`app/services/prompt_cache.py`
  - `get_snapshot(task_type, db)` — 进程级 dict 缓存 RouteSpec + tier，TTL=300s，NEG_TTL=30s；
  - `buffer_track_result(asset_id, success)` — 内存累加计数，不触 DB；
  - `flush_pending_counts()` — 后台 30s 用 `async_session_factory()` 开独立 session 刷盘。
  - `app/main.py` lifespan `start_flusher()` / `stop_flusher()` 在 `engine.dispose()` 之前 drain。

### 强制契约（AGENTS.md 仓库级）

Hot path（`run_text_prompt` / `stream_text_prompt` / SceneOrchestrator / 任意 SSE）**必须**走 `prompt_cache.get_snapshot` + `prompt_cache.buffer_track_result`，不走旧 `PromptRegistry.{get,resolve_route,track_result}`（这套只为 admin CRUD 保留）。

### 未覆盖

上游 LLM provider prompt cache (`AnthropicProvider.generate` 上 `cache_control: ephemeral`、`OpenAIProvider.generate` 上 `prompt_cache_key`) 留作 v1.6 独立优化，与本项是两个互独立的缓存层。

---

## 4. C4 — Cascade auto-regenerate

### 架构

```
chapter_evaluator critical issues
        ↓
cascade_planner — issues_json → [(target_entity_type, target_entity_id, severity)]
        ↓
cascade_tasks (UNIQUE on source_chapter_id, target_entity_type, target_entity_id, severity)
        ↓
celery cascade.run_cascade_task
        ↓
_handle_chapter_target / _handle_outline_target / _handle_character_target / _handle_world_rule_target
        ↓
in-place revision 写到 依附该实体的侧路 JSON 列
```

### Handler 契约

- 全部复用 `_record_inline_cascade_revision(…)` 辅助函数。
- rev_key = `f"{source_chapter_id}:{severity}"`，重复重投 → `status='skipped'`。
- 侧路列：chapter → (沿用 cascade_revisions list)，outline → `content_json.cascade_revisions/cascade_hints` + `version++`，character → `profile_json.cascade_revisions`，world_rule → `metadata_json.cascade_revisions`（新列 alembic `a1001900`）。
- **严禁 handler 修改 cascade_planner ordering 字段**：`outline.is_confirmed` / `name` / `rule_text` / `category`。否则 cascade_tasks 跨轮 UNIQUE 会被击穿。

### 测试 / 烟测

- `tests/test_c4_cascade.py` 56 个用例（+22 unit + 6 outline + 6 character + 7 world_rule + 2 dispatcher routing + 13 planner / surface）。
- `scripts/c4_e2e_smoke.py --rounds 2` double-round 烟测：ROUND1 dispatched=1 worker terminal=`done`；ROUND2 duplicates=1 dispatched=0（完整跨轮幂等验证✅）。

---

## 5. 配套修复 / 升级

- **B1′ evaluator/checker tier-fallback** (commit `6c5224b`)：`ChapterEvaluator.__init__` 调 `await get_model_router_async()` + `generate_with_tier_fallback`，修复自上线以来 evaluator zero LLM call、历史 `chapter_evaluations` 全是 0.0 占位的 P0。`_pick_endpoints_by_tier` + safety net 使 tier 内多 endpoint 全枚举。
- **B2′ Character/HAS_STATE 统一 writer + 49 GqlStatus warning 归零** (commits `a325157` / `0661d96` / `159c66a`)：`EntityTimelineService` 为唯一 writer；新 Celery task `entities.extract_chapter` + Neo4j `ExtractionMarker` 幂等。
- **B2 Recommendation badge 软警卫** (commit 未提供单独 sha，API 级)：`POST/PUT /api/prompts` 增 `confirm_mismatch` query 参，mismatch 返 409 + 结构化 detail，前端 `savePromptWithGuard` 拦截弹 `window.confirm`。
- **Bug K chapter SSE auto-save** (Phase A5)：`generate_chapter` 镜像 outline auto-save pattern 为过去漏写的 chapter SSE 补 `collected_text` + 独立 session UPDATE + `status:saved` 事件。
- **A4.1 outline level invariant** (alembic `a1001500_v150_outline_book_unique`)：修 `level=from_reference` 误用 + `partial UNIQUE (project_id) WHERE level='book'` 贯中`MultipleResultsFound`根因。
- **B1 tier-aware fallback chain** (alembic `a1001501`)：`llm_call_logs` +`tier_used / fallback_reason / attempt_index`。

---

## 6. 测试套件

- pytest 222 passed in 4.13s（3 轮稳定，0 flake）。
- **D-1**：`backend/pyproject.toml` 首次加 `[tool.pytest.ini_options]`：`asyncio_mode="strict"` + `asyncio_default_test_loop_scope="session"` + `asyncio_default_fixture_loop_scope="session"`，溈灭 4× `Future attached to a different loop` flake。
- **D-2**：`tests/test_v10_observability.py` `http_request_total` → `http_requests_total`。

---

## 7. Schema 迁移 (v1.4.x → v1.5.0)

| revision | 作用 |
|---|---|
| `a1001500` | `outlines` UNIQUE `(project_id) WHERE level='book'`，清理 stale `from_reference` |
| `a1001501` | `llm_call_logs` + `tier_used` / `fallback_reason` / `attempt_index` |
| `a1001600` | `prompt_assets` 种入 `scene_planner` (standard/4096) + `scene_writer` (flagship/8192) |
| `a1001601` | 为上述两个 prompt 回填 `endpoint_id` |
| `a1001700` | 新表 `evaluate_tasks` (异步 evaluator API 载体) |
| `a1001800` | 新表 `cascade_tasks` + UNIQUE `(source_chapter_id, target_entity_type, target_entity_id, severity)` |
| `a1001900` | `world_rules` + `metadata_json JSON NOT NULL DEFAULT '{}'` |

升级方式：`alembic upgrade head`。不需手动 backfill；a1001500 内含数据迁移、a1001601 为补丁 endpoint NULL 动作。

---

## 8. 已知限制 / Carry-forward

1. Bug H worker event-loop reuse warning（reprocess 工作流） → v1.6 候选。
2. Qdrant `style_samples_redacted` 3× 冗余点 → v1.6 cleanup。
3. 上游 prompt cache (Anthropic `cache_control: ephemeral` / OpenAI `prompt_cache_key`) → v1.6 独立任务。
4. scene_mode 观测面（`_fallback_scene_briefs` hit-rate 、revise rounds 分布） → v1.6 observability。
5. cascade_tasks UI 面板 → v1.6 前端。
6. 真 LLM scene_mode + auto-revise e2e 烟测未在 v1.5.0 验收报告内表达（code path + unit/integration 已覆盖），可随 v1.6 运营阶段补烟。

---

## 9. 升级检查单

- [ ] `git pull && git checkout v1.5.0`
- [ ] `docker compose build`（alembic head ↑ 需重建 image）
- [ ] `alembic upgrade head` → 期望 `a1001900`
- [ ] `docker compose restart celery-worker`（新 task `entities.extract_chapter` / `evaluations.evaluate_chapter` / `cascade.run_cascade_task`）
- [ ] `pytest -q tests/` → 期望 222 passed
- [ ] `celery -A app.tasks.celery_app inspect ping` → OK
- [ ] （可选）运行 `scripts/c4_e2e_smoke.py --rounds 2` 烟测 cascade 幂等

---

## Appendix — Acceptance commit chain

参见 `docs/v1.5.0-acceptance-report.md` Appendix A（22 个主线 commit + 2 个 docs commit）。

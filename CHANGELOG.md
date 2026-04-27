# Changelog

## [1.7.0] - 2026-04-28

v1.7.0 是 v1.5.x → v1.6.0 遗留项的 carry-forward 收尾，详见 `RELEASE_NOTES_v1.7.0.md`。

### 新增 / 改进

- **X2 `_run_async` 路径统一**：`app/tasks/knowledge_tasks.py` (8 call sites) + `app/tasks/style_tasks.py` (1 call site) 的本地 `_run_async()` 改为 thin delegator → `app/tasks/__init__.py:_run_async_safe`（`reset_engine` + `reset_model_router` 前置，`dispose_current_engine_async` finally）。行为零变更、hardening 限定。
- **X3 Qdrant 孤立 slice cleanup**：新 `scripts/cleanup_orphan_qdrant_slices.py`（PG-as-truth、`--dry-run` 默认、`--apply` 批量 200）。一次性回收 `style_samples_redacted` 8280 条孤立点（12393→4113、与 PG=4115 对齐）；beat_sheets/style_profiles 都是 4115/4115 干净。
- **X5 cascade_tasks UI**：新 `GET /api/projects/{pid}/cascade-tasks`（list + `chapter_id`/`status` 过滤 + `limit ∈ [1,500]`）+ `/summary`（status 直方图）+ `/{tid}` 详情；前端新 `CascadeTasksPanel.tsx` + 独立 `/cascade-tasks?project_id=...` 路由，活跃行存在时 15 s 轮询。

### Schema

- 无新增迁移。`alembic head=a1001900`。

### 测试

- pytest **245 passed**（230 + 4 X2 + 5 X3 + 6 X5）。
- frontend `tsc --noEmit` 干净。
- worker 24 h "attached to a different loop" 警告 = 0。
- 真数据 smoke：project `f14712d6` 上 `/cascade-tasks` 返回 v1.5.0 烟测那条 critical/done/outline 行，summary `{done: 1, total: 1}`。

### Breaking / 注意

- 无破坏性变更。
- X5 endpoint 是 read-only；cascade 写入路径仍走 `app/tasks/cascade.py` planner。
- `task_type="unknown"` Prom label 仍未补 → v1.8 候选。
- L3（Notion 同步审计）按 King 指示推迟。

## [1.6.0] - 2026-04-27

v1.6.0 专注于 prompt cache plumbing + scene mode observability，详见 `RELEASE_NOTES_v1.6.0.md`。

### 新增 / 改进

- **Y1 Anthropic prompt cache**：`AnthropicProvider` 在 system 消息长度≥ `ANTHROPIC_CACHE_MIN_CHARS`（4096）时，自动为 system block 加 `cache_control:ephemeral`。env：`ANTHROPIC_PROMPT_CACHE_ENABLED` (默认 true)。
- **Y2 OpenAI prompt cache key**：`OpenAIProvider` 向 `chat.completions.create` 的 `extra_body` 注入 `prompt_cache_key="{task_type}:{model}"`，最大化 prefix share。env：`OPENAI_PROMPT_CACHE_ENABLED` (默认 true)。
- **Y3 Cache token Prom counter**：`llm_cache_token_total{task_type, provider, model, kind=cache_create|cache_read|cache_uncached}`。`_record_cache_tokens` best-effort。
- **Y4 Baseline cache_uncached emit**：即使上游代理不返 `cached_tokens` 字段，只要 `_OPENAI_CACHE_ENABLED` 且 `prompt_tokens>0`，依然 inc `kind=cache_uncached`，保证运维 baseline 可见。`cache_read` 依然不伪造。
- **X4 Scene mode observability**：新增 `scene_plan_fallback_total{reason}`、`scene_count_per_chapter` (Histogram, buckets 1..12)、`scene_revise_round_total{outcome}` 三类 Prom 指标，注入 `scene_orchestrator.py` + `api/generate.py` revise loop。
- **X1 v1.5.0 acceptance close-out**：`docs/v1.5.0-acceptance-report.md` 补 Appendix B（chapter 5 端到端 SSE + PG truth）；`docs/v1.5.x-v1.6.0-roadmap.md` 补 Status 列。

### Schema

- 无新增迁移。`alembic head=a1001900` 与 v1.5.0 相同。

### 测试

- pytest 230 passed（v1.5.0 的 226 + Y1+Y2+Y3 的 4 + X4 的 3 − 重复 4 + Y4 baseline 1）。
- chapter 6 smoke ✅（scene_count_per_chapter populated）；chapter 7 smoke ✅（`llm_cache_token_total{kind=cache_uncached}` populated）。

### Breaking / 注意

- 无破坏性变更。设 `ANTHROPIC_PROMPT_CACHE_ENABLED=false` 或 `OPENAI_PROMPT_CACHE_ENABLED=false` 可退回 v1.5.0 行为。
- 上游代理不返 `prompt_tokens_details.cached_tokens` 时 `cache_read` 继续为 0（baseline 限制，代理升级后自动产生命中数据）。
- `task_type="unknown"` label：scene_orchestrator 调用未传 kwarg，不影响 metric 正确性，归 v1.7。

## [1.5.0] - 2026-04-27

v1.5.0 在 v1.4 tier routing 上新增 **scene-staged writing + auto-revise loop + prompt cache (双层防死锁) + cascade auto-regenerate** 四大主线，详见 `RELEASE_NOTES_v1.5.0.md` 与 `docs/v1.5.0-acceptance-report.md`。

### Schema (alembic)

- `a1001500` — `outlines` partial UNIQUE on `(project_id) WHERE level='book'` + 修复 from_reference 误用
- `a1001501` — `llm_call_logs` 加 `tier_used / fallback_reason / attempt_index`
- `a1001600` — `prompt_assets` 种入 `scene_planner` (standard/4096) + `scene_writer` (flagship/8192)
- `a1001601` — 为上述两个 prompt 回填 `endpoint_id`
- `a1001700` — 新表 `evaluate_tasks`（异步 evaluator API 载体）
- `a1001800` — 新表 `cascade_tasks` + UNIQUE `(source_chapter_id, target_entity_type, target_entity_id, severity)`
- `a1001900` — `world_rules` 加 `metadata_json JSON NOT NULL DEFAULT '{}'`

### 新增 / 改进

- **C1 Scene-staged writing**：`POST /api/generate/chapter` 支持 `use_scene_mode=true` + `target_scenes`，`scene_planner` (standard) → `scene_writer` (flagship) 流水线落入 `app/services/scene_orchestrator.py`，22 个回归用例（`tests/test_c1_scene_orchestrator.py`）。
- **C2 Auto-revise closed loop**：写完 → evaluator 评分 → < `revise_threshold`（默认 7.0）时把 issues 回灌 SceneOrchestrator 重写（默认 N=2 轮），每轮 `asyncio.timeout(900)`；24 个用例（`tests/test_c2_auto_revise.py`）。
- **C3 Prompt cache deadlock 双层防线**：Layer 1 = revise 前 `await db.rollback()`；Layer 2 = `app/services/prompt_cache.py` (`get_snapshot` TTL=300s + `buffer_track_result` 30s 后台 flusher)，hot path **必须**走 prompt_cache（AGENTS.md 仓库级契约）。
- **C4 Cascade auto-regenerate**：critical issue → `cascade_planner` → `cascade_tasks` → Celery `cascade.run_cascade_task` → 章/纲/角/世界规则四类 in-place handler（rev_key 跨轮幂等），56 个用例（`tests/test_c4_cascade.py`）+ `scripts/c4_e2e_smoke.py --rounds 2` double-round 烟测。
- **B1′ evaluator/checker tier-aware fallback**：修复自上线以来 evaluator zero LLM call P0；`generate_with_tier_fallback` + `_pick_endpoints_by_tier` 安全网。
- **B2′ Character/HAS_STATE 统一 writer**：`EntityTimelineService` 为唯一入口，新增 `entities.extract_chapter` celery task + Neo4j `ExtractionMarker` 幂等；49 个 GqlStatus warning 归零。
- **B2 prompt save 软警卫**：`POST/PUT /api/prompts` 增 `confirm_mismatch` query 参数；mismatch 返 409 + 结构化 detail；前端 `savePromptWithGuard` 拦截弹 `window.confirm`。
- **Bug K chapter SSE auto-save**：镜像 outline auto-save pattern 为 chapter SSE 补 `collected_text` + 独立 session UPDATE + `status:saved` 事件。
- **D-pre 测试基础设施收尾**：`backend/pyproject.toml` 首次加 `[tool.pytest.ini_options]`：`asyncio_mode="strict"` + session-scoped loop/fixture，消灭 4× `Future attached to a different loop` flake；`tests/test_v10_observability.py` 修 `http_request_total`→`http_requests_total`；pytest 222 passed in 4.13s（3 轮稳定）。

### Breaking / 注意

- **Hot path contract**：`run_text_prompt` / `stream_text_prompt` / SceneOrchestrator / 任何 SSE 路径必须走 `prompt_cache.get_snapshot` + `prompt_cache.buffer_track_result`，不再走 `PromptRegistry.{get,resolve_route,resolve_tier,track_result}`（保留为 admin CRUD）。
- **pytest-asyncio**：升级后强制 `asyncio_mode="strict"`；async test class 必须 class 级 `@pytest.mark.asyncio`。
- **Cascade UNIQUE invariant**：handler 严禁修改 `cascade_planner` ordering 字段（`outline.is_confirmed` / `name` / `rule_text` / `category`），否则 `cascade_tasks` 跨轮 UNIQUE 会被击穿。

### Carry-forward / 已知限制

- 上游 LLM provider prompt cache（Anthropic `cache_control: ephemeral` / OpenAI `prompt_cache_key`）留 v1.6。
- Bug H reprocess worker event-loop 复用警告 / Qdrant `style_samples_redacted` 3× 冗余点 / scene_mode 观测面板 / cascade_tasks UI → v1.6 候选。
- 真 LLM scene_mode + auto-revise e2e 烟测可作为 v1.6 运营阶段补烟。


本项目遵循语义化版本号（SemVer）。

## [1.4.1] - 2026-04-24

v1.4.1 是 v1.4 的**收尾补丁**，聚焦三处打磨，不触动 tier schema 与路由语义：

### 新增 / 改进

- **Chunk 17 — `/llm-routing/matrix` 字段对齐 + tier helper 单测**：
  - `backend/app/services/model_router.py` 新增 `VALID_TIERS` / `is_valid_tier()` / `compute_effective_tier()` 三件套，作为 `prompt.model_tier ≫ endpoint.tier ≫ 'standard'` 的唯一真源。
  - `backend/app/api/llm_routing.py` 重写为 DB 驱动：对每个 active `PromptAsset` 左联 `LLMEndpoint`，逐行计算 `effective_tier` 和 `overridden`，返回前端 `MatrixRow` 期望的完整字段（`task_type / mode / prompt_id / prompt_name / endpoint_id / endpoint_name / endpoint_tier / model_name / model_tier / effective_tier / overridden`）。非法 `?tier=` 继续回 200 + `error: "invalid tier"`。
  - 新增 `backend/tests/services/test_model_router_tier.py`（18 个 pytest 全通）覆盖三级回落、非法 / 空值拒绝、embedding tier 透传。
- **Chunk 18 — 端点测试可见性**：
  - `backend/app/api/model_config.py::TestResult` 扩展为 `sent_text / request_summary / response_text / response_preview / embedding_dim / response_first_floats`。
  - `test_endpoint` 发送**字面量 `"hi"`**（替代旧的 `"Say hi"`），捕获 anthropic content block 或 openai chat choices；embedding 端点返回 `embedding_dim` + 前 3 位浮点。
  - `frontend/src/app/settings/page.tsx` 在每个端点卡下新增 `data-testid="endpoint-test-panel"`，同时展示 `请求 / 发送 / 响应` 或 `向量 dim=N head=[...]`；失败时显示红色错误体。用户不再只能看到 `XX ms`。
- **Chunk 19 — NVIDIA embeddings 兼容**（`https://integrate.api.nvidia.com/v1/embeddings`）：
  - `backend/app/services/model_router.py` 新增 `NvidiaEmbeddingProvider`，使用 `httpx.AsyncClient` 直接 POST，载荷固定为 `{input: [...], model, modality: ["text"], input_type, encoding_format: "float", truncate: "NONE"}`，响应兼容 OpenAI schema（`data[0].embedding`）。
  - `VALID_PROVIDER_TYPES` 增加 `"nvidia"`；`test_endpoint` 增加 nvidia 分支（走 `NvidiaEmbeddingProvider.embed_one`，返回 dim + head floats）。
  - 前端 `settings/page.tsx` 新增 `NVIDIA Embeddings` 供应商选项，`base_url` 占位符为 `https://integrate.api.nvidia.com/v1`，模型建议列表内置 `nvidia/llama-nemotron-embed-vl-1b-v2` 与 `nvidia/nv-embedqa-e5-v5`。
- **Chunk 20 — CHANGELOG / RELEASE_NOTES_v1.4.1**：本段 + `RELEASE_NOTES_v1.4.1.md` 记录三处补丁；smoke `[38/40]` 覆盖静态 + runtime 断言。

### Fixed

- Endpoint test probe now surfaces empty / reasoning replies with `finish_reason`
  and usage info (previously showed a blank response). Implemented by raising
  `probe_max_tokens` to 256 and synthesizing a fallback message that lists
  `finish_reason`, `usage`, and `reasoning_tokens` with a hint that the endpoint
  is likely a reasoning model.

### Changed

- Default prompt `max_tokens` raised 4096 → 8192 across all four provider
  signatures, `TaskRouteConfig` / `RouteSpec` fallbacks, and the
  `PromptAsset.max_tokens` column default. Outline task types
  (`outline_book` / `outline_volume` / `outline_chapter`) were further raised
  to 16384 to support long-form generation. Alembic head is now `a1001401`
  with existing prompt rows backfilled (outlines → 16384, others → 8192).
- Book outline generation is now staged by default: stage A produces the
  skeleton, then stages B (characters) and C (world) run in parallel and are
  reassembled into the canonical 9-section document. Avoids the long-output
  quality cliff on a single 10K+ char response. `stream=True, staged=False`
  keeps the legacy single-call behavior.
- Streaming book outline now emits structured per-stage SSE progress events
  (`stage_start` / `stage_chunk` / `stage_end` / `error` / `done`) when the
  client opts in with `staged_stream=1` (query param or request body). Stages
  B and C interleave their chunks by arrival via `asyncio.Queue`, and the
  final `done` event carries the reassembled full outline so auto-save stays
  correct. The Workspace UI shows three progress dots (skeleton / characters
  / world) that flip from gray to blue (running) to green (done) as each
  stage completes.
- Volume outline generation now splits meta from chapter summaries: stage V1
  emits only the per-volume meta (integer `chapter_count`, no
  `chapter_summaries`), then stage V2 loops `ceil(chapter_count / 10)` batches
  of 10 chapters each, carrying the V1 meta plus the last 3 previous summaries
  for continuity. `chapter_idx` is normalized to stay strictly contiguous.
  Merged result keeps the legacy `{...meta, chapter_summaries}` shape.
  Stabilizes long volumes (30+ chapters) where the single-call JSON used to
  truncate or drop indices.

### API / schema

- `GET /api/llm-routing/matrix` 响应字段扩展（兼容老消费者：新字段纯加法）。
- `POST /api/model-config/endpoints/{id}/test` 响应字段扩展（同上）。
- `POST/PUT /api/model-config/endpoints` 的 `provider_type` 新增 `nvidia`。
- 无数据库迁移（不改 alembic head，维持 `a1001400`）。

### smoke 矩阵

- `scripts/smoke_v1.sh` 新增 `[38/38]` / `[39/39]` / `[40/40]` 三段（chunk-17/18/19），静态子集绿；runtime 依赖部署一个 NVIDIA endpoint key 时覆盖 provider_type 创建路径。
- `test_model_router_tier.py`：18 passed in 0.04s。

### 兼容性

- 不影响既有 v1.4 行为：`MatrixRow` 旧字段仍返回，旧 `TestResult.success/message/latency_ms` 语义不变，非 nvidia provider 走原路径。
- 不打 git tag，chunk-17 / chunk-18+19 / chunk-20 三次提交构成补丁集。

## [1.4.0] - 2026-04-24

v1.4.0 **LLM tier routing**。引入“endpoint × prompt 两层 tier”模型，使每个 task 能按能力等级而非单一端点路由 LLM；同时在 env flag 控制下拆分了 critic / extractor 并推出 RAG query rewrite 钩子。所有变更默认 **向前兼容**，flag 关闭时行为与 v1.3 等价；**本版不打 git tag**，仅在 CHANGELOG / RELEASE_NOTES 层面记录。

### 新增 / 改进

- **Chunk 1/2 — 规范与实施计划**：`docs/V10_DESIGN.md` + `docs/V10_CHUNKS.md` 完整描述 tier 枚举（`flagship/standard/small/distill/embedding`）、路由优先级`prompt.model_tier ≫ endpoint.tier ≫ 'standard'` 以及分 chunk 的交付顺序。
- **Chunk 3 — alembic `a1001400`**：给 `llm_endpoints` 新增 `tier TEXT`，给 `prompt_assets` 新增 `model_tier TEXT`，两列均为 nullable；`backend/app/models/prompt.py` / `llm_endpoint.py` ORM 同步补字段。升级交互：`docker compose exec backend alembic upgrade head`。
- **Chunk 4 — API tier 读写**：`/api/model-config/*` 和 `/api/prompts/*` 读写 `tier` / `model_tier`。
- **Chunk 5 — `prompt_registry` builtin**：内建 7 个 task_type 的默认 prompt + tier，并提供 `resolve_tier(prompt, endpoint)` 统一解析入口。
- **Chunk 6 — `model_router` tier registry + `GET /api/llm-routing/matrix`**：响应 `{ rows: MatrixRow[], total, tier, error? }`，支持 `?tier=<enum>` 过滤；非法 tier 不走 5xx，而是返回 `error: "invalid tier"` 让 UI 可展示。
- **Chunk 7 — critic 拆分**：`critic_service` 拆为 `critic_hard`（一致性 / 连续性 / OOC）+ `critic_soft`（节奏 / 读者拉力 / anti-AI）；`CRITIC_SPLIT_ENABLED=0` 回落到原单 critic。
- **Chunk 8 — `consistency_llm_check`**：`critic_hard` 命中 consistency 命中时，`CRITIC_CONSISTENCY_LLM_ENABLED=1` 触发一次 LLM 复核并合并分数。
- **Chunk 9 — `settings_extractor` 3-way split**：`characters` / `world_rules` / `relationships` 三路各自独立 tier，`SETTINGS_EXTRACTOR_SPLIT_ENABLED=0` 任一分类失败时回落到单 extractor。
- **Chunk 10 — RAG query rewrite**：`context_pack` L3 前置一次 query rewrite，`RAG_QUERY_REWRITE_ENABLED=0` 时行为与 v1.3 完全一致。
- **Chunk 11 — `scripts/smoke_v1.sh [22/22]`**：8 条新断言覆盖 `alembic current head = a1001400`、tier / model_tier 暴露、`prompt_registry` 7 个 builtin task_type、`/api/llm-routing/matrix` 的 ok / filter / invalid-tier 三条路径。
- **Chunk 12 — settings ModelConfig tier 下拉 + tier 徽章**：`frontend/src/app/settings/page.tsx` 落实 `TIER_OPTIONS` + `TIER_BADGE_CLASS`。
- **Chunk 13 — prompts `model_tier` 列 + tier 徽章 + endpoint 过滤**：`frontend/src/app/prompts/page.tsx` 补下拉列与 tier 徽章，顶部新增按端点过滤。
- **Chunk 14 — `/llm-routing` 路由矩阵页**：`frontend/src/app/llm-routing/page.tsx` 按 `task_type × mode` 分组展示每个 prompt 的 endpoint(tier) / model / effective_tier，overridden 时在右侧标 `*`；顶部支持 tier 过滤，右侧计算总数 / 覆盖数 / 各 tier 计数；`Navbar.tsx` 在 `prompts` 与 `settings` 间插入 `llm-routing` 入口，`lib/i18n/messages.ts` zh/en 加 `nav.llmRouting`（“路由” / “Routing”）。
- **Chunk 15 — `RELEASE_NOTES_v1.4.md` + README 版本 bullet**：记录 tier 枚举 / env flag / API / schema / smoke 矩阵，README 路线图新增 v1.4 指向 release notes。

### Env flags

- `CRITIC_SPLIT_ENABLED=1`（默认开）
- `CRITIC_CONSISTENCY_LLM_ENABLED=0`（默认关）
- `SETTINGS_EXTRACTOR_SPLIT_ENABLED=1`（默认开）
- `RAG_QUERY_REWRITE_ENABLED=0`（默认关）

### Schema / 迁移

- alembic head 从 `a1001300` 推进到 `a1001400`。
- 新增列：`llm_endpoints.tier TEXT`、`prompt_assets.model_tier TEXT`（均 nullable）。

### API

- 新增：`GET /api/llm-routing/matrix`（可选 `?tier=flagship|standard|small|distill|embedding`）。
- 修改：`/api/model-config/*`、`/api/prompts/*` 读写 tier 相关字段。

### smoke 矩阵

- 完整 runtime：[22/22] 全通，含：
  - `alembic current = a1001400`
  - `/api/model-config` 含 `tier`
  - `/api/prompts` 含 `model_tier`
  - `prompt_registry` 7 builtin task_type
  - `/api/llm-routing/matrix` ok / `?tier=standard` / `?tier=bogus` 三条路径
- runtime 手扣验证（chunk-15）：本地空库状态下 `matrix` 返回 `total=0, error=None`，`?tier=standard` 回 `tier="standard", error=None`，`?tier=bogus` 回 `error="invalid tier"`。

### 向前兼容性

- 所有 env flag 关闭时，v1.4 行为与 v1.3 等价。
- 旧端点 / 旧 prompt 的 `tier` / `model_tier` 允许 NULL，`resolve_tier` 默认落在 `'standard'`。
- `/llm-routing` 为新增路由，旧页面不受影响。

### 版本号

- 本版 **不打 git tag**，`APP_VERSION` / `frontend/package.json.version` 暂保持 `1.2.0`；`CHANGELOG.md` / `RELEASE_NOTES_v1.4.md` 作为唯一版本发布记录。

## [1.2.0] - 2026-04-23

v1.2.0 B 系列可观测性 + CI 自动化。给 v1.0/1.1 的骨架+血肉补上神经系统：结构化日志 -> 指标 -> 错误追踪 -> 流水线。所有 v1.2 变更均为非破坏性，schema 未动，向前兼容。

### 新增 / 改进

- **Chunk 24 — 后端结构化 JSON 日志**：新增 `backend/app/observability/logging.py`（loguru JSON sink + stdlib `logging` 拦截 + `_SENSITIVE_KEYS` 脱敏 + Bearer token 正则脱敏），`backend/app/middlewares/request_logging.py`（`RequestLoggingMiddleware` 生成/回显 `X-Request-ID` 头并为每条请求发一行 JSON，包含 method/path/status/latency_ms/user_id/request_id），`main.py` 在启动时 `setup_logging()` 并注册中间件。smoke 新增 [12/12] 五条断言（静态 + 运行时 + 响应头）。
- **Chunk 25 — Prometheus 指标**：扩展 `backend/app/observability/metrics.py`，HTTP counter 按 Prometheus 命名规范从 `http_request_total` 改名为 `http_requests_total`；新增 `CELERY_TASK_TOTAL` / `CELERY_TASK_DURATION` 两族 celery 指标 + `DB_POOL_SIZE` / `DB_POOL_CHECKED_OUT` / `DB_POOL_OVERFLOW` 三 Gauge；`backend/app/tasks/__init__.py` 接入 celery `task_prerun/postrun/failure/retry/revoked` 信号，为每个任务实例用 `time.monotonic()` 计时；`observability/grafana/dashboards/ai-write-overview.json` 指向新命名；`docker compose up -d prometheus grafana` 起 prometheus(9091)+grafana(3001)。smoke 新增 [13/13] 九条断言覆盖源码 + `/metrics` 响应 + 容器状态 + prom 实际 scrape。
- **Chunk 26 — Sentry 接入（可选 DSN）**：`backend/app/observability/sentry_init.py` 补 `_scrub_event(event, hint)` 作为 `before_send` 和 `before_send_transaction`，复用 `logging.redact` 对 `event.request.headers/cookies/query_string/data` 与 `extra/contexts/tags` 脱敏；未设 `SENTRY_DSN` 时静默返回 `False`。前端新增 `frontend/src/sentry.client.config.ts` 浏览器端 shim：读 `NEXT_PUBLIC_SENTRY_DSN`，动态 `import('@sentry/browser')`（包未装则 no-op），`beforeSend` 剥离 URL query string 与敏感请求头，自挂 `window.error` / `unhandledrejection` 监听。smoke 新增 [14/14] 四条断言。
- **Chunk 27 — GitHub Actions CI + smoke 静态子集 + 自签 JWT fixture**：`.github/workflows/ci.yml` 扩为 ruff + mypy（非阻塞基线）+ pytest + next build + compose-validate + 新的 `smoke-static` job（`SMOKE_STATIC_ONLY=1 bash scripts/smoke_v1.sh`）；`backend/tests/fixtures/self_sign_jwt.py` 暴露 `sign_smoke_jwt(subject, ttl_seconds, secret, algorithm)` + pytest `admin_jwt` fixture + `__main__` CLI，smoke 与 CI 共用同一签名路径。`scripts/smoke_v1.sh` 引入 `SMOKE_STATIC_ONLY=1` 网关，把所有 `docker compose exec` / `curl $BASE/api/*` / `/metrics` / prom 查询块改为 SKIP 而非 FAIL，CI 无需起服务容器即可 grep 所有源码级断言。smoke 新增 [15/15] 六条断言（workflow 存在 + pull_request 触发 + 工具链 + smoke 子集调用 + fixture 就位）。

### 版本号

- backend `APP_VERSION` 从 `1.1.0` 提升至 `1.2.0`。
- `frontend/package.json` `version` 从 `1.1.0` 提升至 `1.2.0`。
- 镜像按 `GIT_TAG=v1.2.0` 重建 `backend` 与 `celery-worker`。

### smoke 矩阵

- 完整 runtime（本地 docker compose stack）：44 passed / 0 failed。
- `SMOKE_STATIC_ONLY=1`（CI 模式）：27 passed / 0 failed / 7 skipped。

## [1.1.0] - 2026-04-23

v1.1.0 A 系列骨架->血肉。在 v1.0 骨架之上填充真正可用的国际化、移动端体验、设计令牌一致性与可记忆的工作区布局。

### 新增 / 改进

- **Chunk 20 — 英文 i18n 真翻译 + 设置页语言切换器**：`lib/i18n/messages.ts` 从 stub 扩充到 39 个键位的中/英完整对照，覆盖 app / nav / locale / settings / workspace 五个命名空间；`settings/page.tsx` 与 `settings/layout.tsx` 全量改用 `useT`，并在设置页顶部提供 `LanguageSwitcher` 组件，切换后写入 `ai-write-locale` cookie 并同步 `<html lang>`。smoke 新增 [9/9] 断言语言开关、cookie 名与英文词条存在。
- **Chunk 21 — 移动端核心页落地**：`ProjectListPage` 默认单列栅格、头部 `flex-wrap`、移动内边距 `px-3 md:px-6`；`MobileWorkspace` 新增大纲抽屉（`mobile-outline-drawer` / `mobile-outline-toggle`），在 list / editor / tools / create 之间切换；`Navbar` 在窄视口下显示汉堡菜单并开合抽屉。iPhone SE（380px）视口下无横向滚动。smoke 新增 [10/10]。
- **Chunk 22 — 业务 UI 全面迁入设计令牌**：新建 `lib/graph-palette.ts` 统一关系图调色板（`SENTIMENT_*` / `NODE_*` / `GRAPH_*` / `NODE_COLOR_PALETTE`）；`relationship-graph/page.tsx`、`RelationshipGraph.tsx`、`EditorView.tsx`、`WritingGuidePanel.tsx` 中所有散落的 `#xxxxxx` 与 `rgba(...)` 字面量迁移到 `var(--text)` / `var(--color-info-500)` / `shadow-card` 等令牌或已导出的调色板常量。smoke 新增 [11/11]，通过 grep 保证白名单外无 hex/rgba 残留。
- **Chunk 23 — 工作区布局 per-project 折叠记忆 + 快捷键 + 移动端自动折叠**：`WorkspaceLayout` 新增可选 `projectId` 属性，`localStorage` 键从平键升级为 `ai-write.workspace.{sidebar,panel}-collapsed:<projectId>`，不同项目记忆各自的侧栏/面板折叠状态，缺省时回落到平键以保持向前兼容；新增 `[` / `]` 键盘快捷键切换侧栏与面板（在输入框 / contentEditable 中自动让行）；`matchMedia('(max-width: 767px)')` 在窄视口自动折叠两侧，不会在视口变宽时自动展开以尊重用户意图。smoke [8/8] 扩充三条断言。

### 版本号

- backend `APP_VERSION` 从 `1.0.0` 提升至 `1.1.0`。
- `frontend/package.json` `version` 从 `1.0.0` 提升至 `1.1.0`。
- 镜像按 `GIT_TAG=v1.1.0` 重建 `backend` 与 `celery-worker`。

## [1.0.0] - 2026-04-23

v1.0.0 big-bang 首个正式版。聚焦于用户体验、可观测性与可导出性。

### 新增 / 改进

- **Chunk 12 — 使用配额 & 管理面板**：引入用户额度、402 拦截器与 `/api/admin/usage` 管理接口，管理员可查看每用户的消耗情况；附带 alembic 迁移 `a1001200`。
- **Chunk 13 — 项目一键导出**：支持将项目导出为 EPUB / PDF / DOCX 三种格式，路径形如 `/api/export/projects/<id>.{epub,pdf,docx}`。
- **Chunk 14 — Tailwind v4 设计令牌**：在 `globals.css` 中落地 `@theme` 变量（品牌色 `--color-brand-*`、圆角 `--radius-card`、阴影与排版），为全站视觉统一奠基。
- **Chunk 15 — 国际化脚手架（中 / 英）**：新增 `lib/i18n/messages.ts` 与 `I18nProvider`，通过 `useT` / `useLocale` 切换语言，语言偏好写入 `ai-write-locale` cookie。
- **Chunk 16 — 移动端响应式基建**：`layout.tsx` 导出 `viewport`，`globals.css` 提供 `safe-area-x/top/bottom` 工具类，Navbar 支持汉堡菜单，移动端首屏可用。
- **Chunk 17 — 工作区侧栏可折叠**：`WorkspaceLayout` 左侧主侧栏与右侧面板均可折叠，通过 `usePersistedFlag` 将状态写入 `ai-write.workspace.sidebar-collapsed` 与 `ai-write.workspace.panel-collapsed`。
- **Chunk 18 — 八项核心能力冒烟脚本**：新增 `scripts/smoke_v1.sh`，在 backend 容器内自签 JWT 后跑 10 项断言，覆盖版本、鉴权、额度、导出、设计令牌、i18n、移动端、侧栏等 v1.0 所有关键能力。

### 版本号

- backend `/api/version` 新增 `version` 字段，固定为 `1.0.0`（`APP_VERSION`）。
- `frontend/package.json` `version` 从 `0.1.0` 提升至 `1.0.0`。

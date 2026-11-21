# ai-write 架构说明（v1.7.2 基线）

> **适用版本**：`v1.7.2` （commit `31c0362`，2026-04-28）
> **适用范围**：全栈。运维操作手册看 `RUNBOOK.md`。
>
> 本文检查多少油说多少话。架构隐含的设计意图是：
> 1. **以 ModelRouter 为单一 LLM 入口**，所有 prompt 都走 `prompt_registry` 加载。
> 2. **创作链是单向 DAG**：参考书入库 → 风格萄馕 → 全文/分卷/章节大纲 → scene_orchestrator → 正文 → 评分 → cascade。
> 3. **可观测是合同**：每一环节都要能在 Prometheus 里读出指标，每一次 LLM 调用都要落 `llm_call_logs`。

---

## 1. 技术栈

| 层 | 技术 | 版本 |
|---|---|---|
| 后端 API | FastAPI + uvicorn | Python 3.11 |
| 后台任务 | Celery + Redis | Redis 7 |
| 主库 | PostgreSQL | 16-alpine |
| 向量库 | Qdrant | latest |
| 图库 | Neo4j 5 | community |
| ORM | SQLAlchemy 2.x async | — |
| 迁移 | Alembic，当前 head `a1001900` | — |
| 前端 | Next.js (App Router) + TypeScript | — |
| 反代 | nginx | alpine |
| 观测 | Prometheus + Grafana | 2.54.1 / 11.2.0 |
| LLM 上游 | OpenAI / Anthropic / OpenAI-Compat 代理 | `141.148.185.96:8317` |

## 2. 系统拓扑

```
╔═══════ docker compose (全部绑 127.0.0.1) ══════╗
║                                                       ║
║  nginx  →  frontend (3100->3000)                      ║
║    ↳→  backend  (8000:8000)                           ║
║          ├─ postgres  (5432:5432)                     ║
║          ├─ redis     (6379:6379)                     ║
║          ├─ qdrant    (6333,6334)                     ║
║          ├─ neo4j     (7474, 7687)                    ║
║          └─ LLM upstream  141.148.185.96:8317        ║
║                                                       ║
║  celery-worker (与 backend 同镜像)                      ║
║    └─ 从 redis 拉任务，写 postgres + qdrant            ║
║                                                       ║
║  prometheus (9091->9090) ← backend /metrics            ║
║  grafana    (3001->3000) ← prometheus                  ║
╚══════════════════════════════════════════╝
```

容器名与服务名一一对应：`ai-write-postgres-1` `ai-write-redis-1` `ai-write-qdrant-1` `ai-write-neo4j-1` `ai-write-backend-1` `ai-write-celery-worker-1` `ai-write-frontend-1` `ai-write-nginx-1` `ai-write-prometheus-1` `ai-write-grafana-1`。

## 3. 创作链路径（从参考书到正文）

```
┐──────────────────┐  ┐──────────────────┐  ┐──────────────────┐
│ 参考书上传/爬取 │→ │ 萄馕 × 多层      │→ │ 大纲×三级      │
└──────────────────┘  └──────────────────┘  └──────────────────┘
   reference_books      style_profiles      outlines.level
   reference_book_       beat_sheets         {book,volume,
   slices                style_samples        chapter}
   text_chunks                                + chapters.
                                                outline_json
                                                                  
┐──────────────────┐  ┐──────────────────┐  ┐──────────────────┐
│ 场景调度        │→ │ 正文生成      │→ │ 评分 5 维    │
└──────────────────┘  └──────────────────┘  └──────────────────┘
   scene_orchestrator    chapters.content    chapter_evaluations
                         _text                              ↓
                                          ┐──────────────────┐
                                          │ cascade 回流   │
                                          └──────────────────┘
                                            cascade_tasks
```

### 3.1 参考书入库

- API：`POST /api/decompile/{book_id}/reprocess`, `POST /api/decompile/{book_id}/retry-missing`, `GET /api/decompile/{book_id}/{slices,style-profiles,beat-sheets,decompile-status}`
- 服务：`reference_ingestor.py` (上传+拆分) + `semantic_chunker.py` (语义切片) + `book_source_engine.py` (爬取)
- 任务：`tasks.process_uploaded_book`, `tasks.crawl_book`, `tasks.vectorize_book`, `tasks.batch_test_sources`
- 落库：`reference_books`, `reference_book_slices`, `text_chunks`（PG）、`text_chunks`（Qdrant）

### 3.2 风格萄馕（style extraction & distillation）

- API：`/api/styles/*` 14 个端点（create / update / bind / detect-from-book / detect-by-author / extract-structure / test-write / compile-preview…）
- 服务：
  - `feature_extractor.py`——`PlotFeatures` + `StyleFeatures`，同时抽取词频/句长/节奏特征与纯 LLM 萄馕
  - `style_abstractor.py`——单片 LLM 抽象
  - `beat_extractor.py`——beat sheet 抽取
  - `style_clustering.py` → `style_compiler.py` → `style_runtime.py` (`resolve_style_prompt` / `resolve_anti_ai_prompt`)
- 任务：`tasks.run_style_clustering`, `tasks.extract_features`
- 落库：PG `style_profiles` ｜ Qdrant `style_samples_redacted` `beat_sheets` `style_profiles`
- task_type：`extraction`, `beat_extraction`

### 3.3 大纲三级生成

- API：
  - `POST /api/generate/outline`（状态机入口，能生成三级中的任一级）
  - `POST /api/outlines`、`PUT /api/outlines/{id}`、`POST /api/outlines/{id}/confirm`、`POST /api/outlines/{id}/extract-settings`
  - `POST /api/volumes/{volume_id}/regenerate`
- 服务：`outline_generator.py` (792 行) + `outline_from_reference.py` + `settings_extractor.py`
- task_type：`outline_book` (全文)、`outline_volume` (分卷)、`outline` (章节)
- 落库：`outlines` 表（`level` 列上唯一约束：每个 project 只能一份 `book` outline）+ `chapters.outline_json`

### 3.4 场景调度与正文生成

- API：`POST /api/generate/chapter`（同步/SSE）、`POST /api/generate/async`（Celery）、`GET /api/generate/async/{task_id}`
- 服务：
  - `chapter_generator.py` (98 行薄包装)
  - `scene_orchestrator.py` (405 行) —— 拆 scene_briefs → 逐场生成 → 拼接
  - `text_pipeline.py`、`text_rewriter.py`、`auto_revise.py`、`anti_ai_scanner.py`、`memory.py`、`memory_compactor.py`、`context_pack.py`、`ctxpack_cache.py`
- 任务：`tasks.run_async_generation`, `tasks.run_pipeline_generation`
- task_type：`generation` (主体)、`polishing`, `redaction`, `compact`, `summary`, `critic`
- 落库：`chapters.content_text/word_count/status` + `generation_runs` + `generation_tasks`
- 关键指标：`scene_count_per_chapter`、`scene_plan_fallback_total{reason}`、`scene_revise_round_total{outcome}`、`generation_run_phase_total`

### 3.5 评分

- API：`POST /api/evaluate/start`, `GET /api/evaluate/tasks/{task_id}`
- 服务：`chapter_evaluator.py` (278 行)。输出 `EvaluationResult` 含 5 维分 + overall + `issues_json`
- 任务：`evaluation_tasks.EVALUATE_CHAPTER_TASK`
- task_type：`evaluation`
- 落库：`chapter_evaluations`（`plot_coherence` `character_consistency` `style_adherence` `narrative_pacing` `foreshadow_handling` `overall` + `issues_json` JSON）

### 3.6 cascade 回流

- 触发：评分完成 → `chapter_evaluator` 检测到 `IN_SCOPE_DIMENSIONS` (`plot_coherence`/`foreshadow_handling`/`character_consistency`) 上累计 ≥ `CRITICAL_ISSUE_COUNT=3` 个 `severity∈{high,critical}` issue，或 overall ≤ `DEFAULT_OVERALL_THRESHOLD=9.0`
- API：`GET /api/projects/{pid}/cascade-tasks{,/summary,/{tid}}`
- 服务：`cascade_planner.py` (420) → `cascade_regenerator.py` (251)
- 任务：`cascade.RUN_CASCADE_TASK = "cascade.run_cascade_task"`，处理锁失败 `LOCK_RETRY_COUNTDOWN=30` 秒
- 落库：`cascade_tasks`（`source_chapter_id, source_evaluation_id, target_entity_type ∈ {chapter,outline,character,world_rule}, severity ∈ {high,critical}, status ∈ {pending,running,done,failed,skipped}, parent_task_id` + UNIQUE(source_chapter_id, target_entity_type, target_entity_id, severity)）

### 3.7 SSE 事件总线

章节评分与 cascade 返回事件：`evaluating / scored / revise_skipped / revising / revise_error / cascade_triggered`。

## 4. 后端模块总览

### 4.1 `app/api/`（FastAPI router，共 35 个文件）

分为六类：

- **项目/设置**：`projects` `settings` `auth` `model_config` `prompts` `llm_routing` `lora` `vector_store` `filter_words` `admin_usage`
- **创作主线**：`outlines` `volumes` `chapters` `generate` `pipeline` `evaluate` `cascade` `rewrite` `variants` `version(s)` `export`
- **参考书/风格**：`knowledge` `decompile` `styles`
- **指标/数据**：`metrics` `quality` `call_logs` `generation_runs` `run_bus`
- **调试辅助**：`debug` `ask_user` `changelog` `foreshadows`
- **底层**：`writing_engine`

### 4.2 `app/services/`（65 个文件）

按职责分组：

- **创作链**：`outline_generator` `outline_from_reference` `chapter_generator` `scene_orchestrator` `text_pipeline` `text_rewriter` `auto_revise` `chapter_evaluator` `cascade_planner` `cascade_regenerator` `batch_generator`
- **上下文与内存**：`context_pack` `ctxpack_cache` `memory` `memory_compactor` `prompt_cache` `semantic_cache`
- **人设/实体**：`entity_dispatch` `entity_redactor` `entity_timeline` `strand_tracker` `foreshadow_manager` `hook_manager` `plot_structure`
- **风格与反-AI**：`feature_extractor` `style_abstractor` `beat_extractor` `style_clustering` `style_compiler` `style_runtime` `style_detection` `anti_ai_scanner`
- **参考书入库**：`reference_ingestor` `semantic_chunker` `book_source_engine` `qdrant_store` `rag_rebuild`
- **LLM 路由**：`model_router` `prompt_registry` `prompt_loader` `prompt_recommendations` `llm_call_logger` `lora_manager`
- **质量与结构检查**：`quality_scorer` `critic_service` `constory_checker` `genre_rules` `writing_guides` `writing_engine_seed` `bvsr` `tool_registry`
- **运行面**：`generation_runner` `pipeline_service` `run_bus` `incremental_sync` `version_control` `change_log` `export_service` `usage_service` `budget_allocator` `ask_user_service`
- **子目录**：`agents/`（multi-agent prompt 架构下的代理）、`checkers/`（一致性检查器合集）

### 4.3 `app/tasks/`（6 个文件）

| 任务名 | 文件 | 作用 |
|---|---|---|
| `tasks.run_style_clustering` | `style_tasks.py` | 风格聚类与萄馕 |
| `tasks.vectorize_book` | `knowledge_tasks.py` | 参考书向量化 |
| `tasks.run_async_generation` | `knowledge_tasks.py` | 章节异步生成 |
| `tasks.run_pipeline_generation` | `knowledge_tasks.py` | 管道式生成 |
| `tasks.process_uploaded_book` | `knowledge_tasks.py` | 上传书预处理 |
| `tasks.crawl_book` | `knowledge_tasks.py` | 爬取书（带重试 max=3）|
| `tasks.batch_test_sources` | `knowledge_tasks.py` | 源批量检查 |
| `tasks.extract_features` | `knowledge_tasks.py` | 特征抽取 |
| `tasks.run_quality_score` | `knowledge_tasks.py` | 质量评分 |
| `evaluation.evaluate_chapter` | `evaluation_tasks.py` | 章节评分 |
| `cascade.run_cascade_task` | `cascade.py` | cascade 执行 |
| `entities.extract_chapter` | `entity_tasks.py` | 实体抓取 |

### 4.4 `app/models/`（SQLAlchemy ORM）

`ask_user` `call_log` `cascade_task` `decompile` `generation_run` `generation_task` `pipeline` `project` `prompt` `settings_change_log` `usage_quota` `writing_engine`。主表设计看迁移 `alembic/versions/`。

### 4.5 LLM 路由层（`model_router.py`, 1157 行）

- **公开方法**：`generate / generate_stream / generate_by_route / stream_by_route / generate_with_tier_fallback / stream_with_tier_fallback / generate_with_fallback`
- **路由读取**：`task_routing: dict[str, TaskRouteConfig]`（启动时从 DB 加载）
- **三档降级**：每个 task 按 tier 顺序（默认 standard → fallback）逐个尝试 endpoint
- **task_type 传递**（v1.7.1 Z1）：所有公开方法都接受 `task_type=` 并传进 provider 的 `**kw`
- **`time_llm_call` 包裹**（v1.7.2 Z3）：4 个主方法共 8 个 provider 调用点都在 `with time_llm_call(task_type, provider.__class__.__name__, model)` 里，保证 `llm_call_total / llm_call_duration_seconds / llm_token_total` 全路径发
- **日志**：`_log_meta` 分支会下沉到 `llm_call_logger`，写 `llm_call_logs` 表

### 4.6 task_type 完整列表

项目里出现过的 task_type（含含义）：

| task_type | 含义 |
|---|---|
| `outline_book` | 全文大纲 |
| `outline_volume` | 分卷大纲 |
| `outline` | 章节大纲 / 通用大纲 |
| `generation` | 章节正文生成 |
| `polishing` | 改写 |
| `redaction` | 脱敏 / 隐匿 |
| `compact` | 记忆压缩 |
| `summary` | 摘要 |
| `critic` | 评审 |
| `evaluation` | 五维评分 |
| `extraction` | 特征/设定抽取 |
| `beat_extraction` | beat 抽取 |
| `consistency_llm_check` | 一致性检查 |
| `by_route` | `generate_by_route` 默认值 |
| `by_route_stream` | stream 默认值 |

## 5. 观测层

### 5.1 指标清单（`app/observability/metrics.py`）

所有 LLM Counter/Histogram 注册到自定义 `REGISTRY = CollectorRegistry(auto_describe=True)`：

| 名称 | 类型 | 标签 |
|---|---|---|
| `http_requests_total` | Counter | method / path_template / status |
| `llm_call_total` | Counter | task_type / provider / model / **status∈{ok,error}** |
| `llm_call_duration_seconds` | Histogram | task_type / provider / model（梶 0.5–1 –2–5–10–20–30–60–120–300s） |
| `llm_cache_token_total` | Counter | task_type / provider / model / kind∈{cache_create, cache_read, cache_uncached} |
| `llm_token_total` | Counter | task_type / provider / model / direction∈{input,output} |
| `generation_run_phase_total` | Counter | phase / status |
| `scene_plan_fallback_total` | Counter | reason∈{unparseable, too_few} |
| `scene_count_per_chapter` | Histogram | —（1–12） |
| `scene_revise_round_total` | Counter | outcome |

采集路径：backend `/metrics` → prometheus → grafana 面板。

### 5.2 关键业务常量

- `IN_SCOPE_DIMENSIONS = {plot_coherence, foreshadow_handling, character_consistency}`
- `CRITICAL_ISSUE_COUNT = 3`
- `DEFAULT_OVERALL_THRESHOLD = 9.0`
- `LOCK_RETRY_COUNTDOWN = 30` (cascade)
- `TARGET_ENTITY_TYPES = (chapter, outline, character, world_rule)`
- `SEVERITIES = (high, critical)`
- `STATUSES = (pending, running, done, failed, skipped)`

## 6. 前端结构

- 技术：Next.js App Router (`src/app`) + TypeScript + React + Tailwind
- 账号/鉴权：`lib/api.ts:apiFetch<T>(path, options?)`，401 → clearToken + redirect `/login`
- 工作区：`components/workspace/{DesktopWorkspace,MobileWorkspace,WorkspaceLayout}.tsx`
- 面板（`components/panels/`）：`AntiAIPanel` `CascadePanel(LEGACY)` `CascadeTasksPanel` `CheckerDashboard` `EvaluationPanel` `ForeshadowPanel` `GeneratePanel` `RelationshipGraph` `SettingsPanel` `StrandPanel` `StylePanel` `TokenDashboard` `VersionPanel` `WritingGuidePanel`
- 检查：`npx --no-install tsc --noEmit`——空输出 = 干净。当前 **无单元测试**。

## 7. 关键表 / 迁移历史

迁移 head：`a1001900`。里程碑（选要者）：

- v0.5：`a0504000_v05_prompt_routing` 打底提示词路由
- v0.6：`a0600000_v06_decompile` 参考书拆解
- v0.8/0.9：`writing_engine` + `settings_graph`
- v1.4.x：`v141_prompt_max_tokens`
- v1.5.0：`scene_writing` 场景表、`evaluate_tasks`、`outline_book_unique`、`llm_call_logs_tier_fallback`
- v1.5.1+：序号推到当前 `a1001900`

全量表列表考 RUNBOOK “变更与迁移” 部分或直接 `\dt` 看。

## 8. 已知短板（调查时会遇到的坑）

1. **核心创作服务无单测覆盖**：`outline_generator / style_abstractor / feature_extractor / beat_extractor / chapter_generator / settings_extractor` 都是生产 smoke 在兼底。`P0` 待补。
2. **`model_router.py` 单文件 1157 行**：迁移到 v1.8 可考虑拆分。
3. **前端零单测**：仅有 `tsc --noEmit` 厄住类型。
4. **无静态分析门禄**：`ruff` `mypy` `pip-audit` 从未跑过，覆盖率未知。会在即将到来的 `AUDIT_BASELINE` 完成后填平。
5. **`llm_call_logs` 表无 `provider` 列**：查询 SQL 时别奉 “按 provider 聚合” 的 都能走 SQL，应该走 Prom 。
6. **scene_orchestrator 体量不稳定**：ch3 = 23 字 / ch4 = 14337 字 是已观测到的边缘 case，变更集中在该服务时需加回归用例。

## 9. 变更与迁移阳光

- 发布节奏：按 v1.x.y SemVer，每次 tag annotated，`RELEASE_NOTES_vX.Y.Z.md` + `CHANGELOG.md` 同步。
- v1.7.2 在优化 LLM 观测。Z3 后 `llm_call_*{provider}` 字符串变为类名（如 `OpenAICompatProxy`）——这是全局一次性的 Prom 标签调整，面板如果硬匹配了旧字符串需一起改。

---

## 附录 A：项目目录结构

```
ai-write/
  AGENTS.md             # 根仓 代理交接说明
  README.md             # 项目入口
  CHANGELOG.md          # 按发布书写的变更日志
  ITERATION_PLAN.md     # 锦途路线
  RELEASE_NOTES_vX.md   # 每个 tag 一份
  docker-compose.yml    # 拓扑唯一口
  docs/                 # 包含本文件与 RUNBOOK
  scripts/              # backup.sh / smoke_v1.sh / cleanup_orphan_qdrant_slices.py / seed_*
  postgres-init/        # postgres 首次初始化
  observability/        # prometheus.yml + grafana provisioning
  evaluate/             # 评估集/脚本
  backups/              # backup.sh 输出位置
  nginx/                # 反代配置
  backend/
    alembic/            # 迁移 (versions/, env.py)
    app/
      api/              # 35 个 router
      services/         # 65 个 service + agents/ + checkers/
      tasks/            # 6 个 celery 文件
      models/           # 12 个 ORM
      schemas/          # pydantic
      observability/    # metrics.py
      db/               # async session
      graphs/           # langgraph DAG
      middlewares/      # 鉴权/限速等
      scripts/          # 运维脚本
      utils/            # 工具
    tests/              # 252 passed in v1.7.2
  frontend/
    src/
      app/              # Next.js App Router
      components/
        workspace/      # 3 个布局
        panels/         # 14 个面板
      lib/api.ts        # apiFetch<T>
```

## 附录 B：本架构文档的维护约定

- 每次新增主要服务 / API / 任务 / 指标要一同更新本文 §2–6。
- 每次发布（即一次 tag）后顶部 “适用版本” 要动。
- 最小粒度“变动 commit” 可以只动一两节，不需要重写整份。

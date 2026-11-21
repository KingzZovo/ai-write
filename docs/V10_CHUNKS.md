# v1.0.0 分块执行计划

基于 `docs/V10_DESIGN.md`，拆成 20 个独立可发 commit。每个 chunk 单独过 tsc/ast/alembic/next build 后提交。

## Phase 1 — 基建（无 LLM 依赖，可立刻验证）

### Chunk 0 — plan doc（本 commit）
建分支 `feature/v1.0-big-bang`，本文件落盘。

### Chunk 1 — 镜像瘦身 + 版本标记
- `backend/Dockerfile` 改 multi-stage（builder 装依赖 → runtime 只 copy site-packages + app）
- `GIT_SHA` + `GIT_TAG` build-arg 写入 `/app/.git_sha`
- `app.main` 读 `.git_sha` 暴露到 `GET /api/version`
- 目标：backend image < 1GB（当前未测）；`docker exec backend cat /app/.git_sha` 可读

### Chunk 2 — Prometheus 埋点 + 看板
- 依赖：`prometheus-client`
- 指标：`llm_call_total{task_type,provider,status}` / `llm_call_duration_seconds` / `generation_run_phase_total{phase}` / `http_request_duration_seconds`
- 中间件：FastAPI `Middleware` + `run_text_prompt`/`run_structured_prompt` 包一层
- 端点：`GET /metrics`（不鉴权，仅 intranet）
- compose：`prometheus` + `grafana` 服务 + 预装 1 张 JSON dashboard
- 目标：Grafana 能看到 llm 调用次数 + p95 延迟

### Chunk 3 — Sentry 接入
- 依赖：`sentry-sdk[fastapi,celery]`
- 读 `SENTRY_DSN` env，空则 skip init
- FastAPI + Celery 集成
- 触发一个 `/api/debug/sentry` 测试路由（rate-limited）

### Chunk 4 — GitHub Actions CI
- `.github/workflows/ci.yml`：PR 时跑 ruff + pytest + tsc --noEmit + next build
- `.github/workflows/release.yml`：push tag `v*` 时 build image → GHCR
- pre-commit 配置（ruff、prettier）

### Chunk 5 — 测试补齐
- 修 `conftest.py` 的 asyncpg event_loop 冲突
- API 集成测试：`projects` / `chapters` / `volumes` / `relationships` / `changelog` CRUD
- 前端纯函数 unit：`detectVolumeCount` / `parseVolumeOutline`
- 目标：`pytest` / `npm test` 全绿

### Chunk 6 — 备份脚本 + Celery beat
- `scripts/backup.sh`：`pg_dump` + `qdrant snapshot` + `redis bgsave` + `neo4j dump` → tar.gz
- 可选 S3 上传（`BACKUP_S3_BUCKET`）
- Celery beat daily 03:00 UTC

## Phase 2 — 新能力（需 LLM / 新编排）

### Chunk 7 — BVSR 抽卡
- 新表 `chapter_variants`（alembic a1000700）：id / chapter_id / run_id / round / text / score / critic_report_id / selected / created_at
- drafting 节点 loop N（env `BVSR_N`，默认 3）
- 每份 critic → score = hard×10 + soft×2 + ai_trap×5
- 最低分进 rewrite，其余入库
- API：`GET /chapters/{id}/variants`、`POST /variants/{id}/select`
- 前端章节页 "候选" Tab：3 份文本 + 分数 + 一键切换

### Chunk 8 — LangGraph runner
- 依赖：`langgraph`、`langgraph-checkpoint-postgres`
- `app/graphs/generation_graph.py`：StateGraph 节点 plan/recall/draft/critic/rewrite/finalize/compact
- 条件边：`CriticReport.hard_count > 0` → rewrite，否则 finalize
- 检查点存 postgres（迁移 a1000800 建 `langgraph_checkpoints` 表）
- Feature flag `LANGGRAPH_RUNNER_ENABLED`（默认 off）
- `GET /api/generation-runs/{id}/graph` 返 DOT
- 前端 `/runs/{id}` 页 react-flow 渲染

### Chunk 9 — 多 Agent Teams
- 3 个新 task_type 种子进 `prompt_assets`：`planner` / `writer` / `editor`
- Consistency 沿用现有 critic
- LangGraph 中 Writer 出 N draft 时 Editor 异步跑
- Agent 间消息：Redis Stream `run:{id}:bus`
- `/api/runs/{id}/bus` SSE 订阅前端可看实时协作

### Chunk 10 — Neo4j + ConStory v1
- `neo4j` 已在 compose，现接入 service `app/services/neo4j_store.py`
- 建 schema：`(:Character)-[:AT]->(:Location {at:datetime})`、`(:Character)-[:OWNS]->(:Item)`
- 章节 finalize 时抽取事件写入 neo4j
- 3 checker（time_reversal / geo_jump / item_missing）接入 critic pipeline
- 产出作为 critic_report `issues[].type="consistency_*"` 展示

## Phase 3 — 内容库 & 计费

### Chunk 11 — 10 套题材画像种子
- `scripts/seed_genre_profiles.py`：idempotent upsert
- 10 套：仙侠·洪荒 / 仙侠·现代 / 都市·赘婿 / 都市·重生 / 科幻·末世 / 科幻·星际 / 玄幻·异世界 / 悬疑·探案 / 历史·架空 / 游戏·无限流
- 每套：20+ writing_rules / 30+ beat_patterns / 50+ anti_ai_traps
- 启动时自动跑 seed（或 `alembic upgrade` 后单独 `python -m scripts.seed_genre_profiles`）

### Chunk 12 — 配额 & 成本账本
- 新表 `usage_quotas`（alembic a1001200）：user_id / period('month') / token_limit / token_used / cost_limit_cents / cost_used_cents
- 拦截层：`llm_call_logger` 前查配额，超限抛 402 `QuotaExceeded`
- `POST /api/admin/usage-quotas` 设置、`GET /api/admin/usage` 看汇总
- 前端 `/admin/usage` 页：按用户 / 项目 / 模型统计

### Chunk 13 — 导出 EPUB / PDF / DOCX
- 依赖已有：`ebooklib`。新加 `weasyprint`、`python-docx`
- `POST /api/projects/{id}/export?format=epub|pdf|docx`
- 异步 Celery 任务 → 文件落 `/app/exports/` → `GET /api/exports/{id}` 下载
- 前端项目页加 "导出" 下拉

## Phase 4 — 前端收尾

### Chunk 14 — 设计 tokens + 暗色主题
- `frontend/src/styles/tokens.css`：色板 / 间距 / 字号
- Tailwind config 引用 tokens
- 暗色主题 class 全站覆盖审查

### Chunk 15 — i18n zh-CN / en
- 依赖：`next-intl` 或 `react-i18next`
- 所有硬编码中文抽进 `frontend/src/i18n/{zh-CN,en}.json`
- 右上角语言切换

### Chunk 16 — 移动端响应式
- 核心页：项目 / 章节 / 设定集 / 知识库 / 4 个 v0.9 新页
- 断点 <768px 单栏布局 + 抽屉导航

### Chunk 17 — workspace 侧栏 nav + 角色详情结构化
- 侧栏加入 4 个 v0.9 页入口
- 角色详情拆：能力成长表 / 装备时间线 / 外观档案 / 关系列表（替代 JSON textarea）
- 关系图支持拖线建边 + 右键删

## Phase 5 — 发布

### Chunk 18 — 验收 smoke + 文档
- 8 条 V10_DESIGN 验收逐条跑
- `ITERATION_PLAN.md` 更到 v1.0.0
- `CHANGELOG.md` 汇总 v0.6 → v1.0 全部

### Chunk 19 — merge + tag + push + 部署
- `git checkout main && git merge --no-ff feature/v1.0-big-bang`
- `git tag -a v1.0.0`
- `git push origin main v1.0.0`
- `docker compose up -d --build` 全栈
- 部署自检：alembic head / routes 数 / /metrics / /api/version / prometheus+grafana up / frontend 4 新页 200

## 新迁移预留

- `a1000700` — `chapter_variants`
- `a1000800` — `langgraph_checkpoints`
- `a1001000` — Neo4j side channel, may not need pg migration
- `a1001200` — `usage_quotas`

## 回滚策略

每个 chunk 是独立 commit；Phase 2+ 带 feature flag（`BVSR_N=0` / `LANGGRAPH_RUNNER_ENABLED=false` / `CONSTORY_ENABLED=false`）可关闭。迁移自 a0900000 线性递增，可逐条 downgrade。

## 体量估计

约 20 chunks / 20-25 天原计划、本代理会话中按需拆多轮 resume 推进。每轮至少推 1-3 chunk 并 commit。

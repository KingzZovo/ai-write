# v1.0.0 — 高级工作流 + 多 Agent 协作 + 生产就绪

**目标：** 把 v0.6-v0.9 的基建升级为“真正能长跑的产品”。编排替换为 LangGraph，生成升级为 BVSR 抽卡 + 多 Agent 协作，运行环境达生产标准（监控、配额、CI/CD、瘦镜像）。

## 1. LangGraph 工作流编排

### 取代对象

`generation_runner.execute_run` 里的 `if phase == ...` 链式 dispatch。

### 新模型

- `GenerationGraph`：用 `langgraph.StateGraph` 描述
- 节点：`plan` / `recall` / `draft` / `critic` / `rewrite` / `finalize` / `compact`
- 边：条件边基于 `CriticReport.hard_count` 分叉
- 持久化：`langgraph.checkpoint.postgres` 替换现在的 `checkpoint_data` JSONB
- 可视化：`/api/generation-runs/{id}/graph` 返回 DOT，前端用 react-flow 渲染

### 迁移

- 兼容层：老 `generation_runs` 数据仍可读；新 run 走 LangGraph
- Flag：`LANGGRAPH_RUNNER_ENABLED`（默认 off，灰度切换）

## 2. BVSR 多版本抽卡

**B**ranch **V**ariants **S**core **R**ank — 为每一章 drafting 生成 N 个候选。

- `drafting` 节点循环 N 次（默认 3）→ N 份 draft
- 每份走一次 `critic`（含 anti-AI 扫描）→ 评分（hard×10 + soft×2 + ai_trap×5）
- 取最低分的 draft 进 rewrite；其余存入 `chapter_variants` 表
- 前端章节页新增“查看候选”Tab，作者可手动选

### 新表

`chapter_variants`：id, chapter_id, run_id, round, text, score, critic_report_id, selected(bool), created_at

## 3. 多 Agent 协作（Teams 模式）

### 角色

- **Planner**（外策划）：吃大纲 + 上一章结尾 → 本章骨架
- **Writer**（作者）：吃骨架 + ContextPack → draft（可走 BVSR）
- **Editor**（内策划 / 编辑）：只看 draft → 指出“节奏太慢 / 对话太水”并返回修改建议
- **Consistency**（Critic）：沿用 v0.7 的 critic_service

### 编排

- LangGraph 中并行：`Writer` 出 N 个 draft 时，`Editor` 对每个异步评分
- Agent 之间通过 Redis Stream 传 message（`run:{id}:bus`）
- 每个 Agent 有独立 system prompt（task_type：`planner` / `writer` / `editor`），新种子进 `prompt_assets`

## 4. 题材规则库 v1

基于 v0.8 的 `genre_profiles`，交付 10 个精调画像：

仙侠·洪荒 / 仙侠·现代 / 都市·赘婿 / 都市·重生 / 科幻·末世 / 科幻·星际 / 玄幻·异世界 / 悬疑·探案 / 历史·架空 / 游戏·无限流

每个画像预装：
- 20+ `writing_rules`
- 30+ `beat_patterns`（开局 / 卷末 / 大高潮 / 结局各 5-8 条）
- 50+ `anti_ai_traps`（中文网文高频 AI 味）
- 默认 `outline_book` 示例模板

## 5. ConStory-Checker 深度一致性

在 v0.7 Critic 基础上扩：

- 跨章节时间线一致性：时间推进倒流检测
- 地理一致性：角色在 A 地，下一章不能直接 B 地（需中间章铺垫）
- 物品一致性：法宝/装备 Issue 后不能凭空消失
- 使用 Neo4j 做时序 + 位置图谱查询（neo4j 已在 compose 里但未接入）

## 6. 生产就绪

### 镜像瘦身

- backend Dockerfile 换 `python:3.11-slim` → multi-stage
- 预期：镜像从 ~2.5GB 降到 < 800MB

### 监控

- `prometheus_client` 埋点：`llm_call_total` / `llm_call_duration_seconds` / `generation_run_phase_total`
- Grafana 看板（docker-compose 加 prometheus + grafana）
- Sentry 集成（FastAPI + Celery）

### CI/CD

- GitHub Actions：
  - PR：lint + `pytest` + `npx tsc --noEmit` + `npx next build`
  - push main：build images → push GHCR → `docker compose pull && up -d` 到一台预置 runner
- `docker compose config --quiet` 作为健康检查

### 配额 & 成本账本

- 新表 `usage_quotas`：user_id, period(`month`), token_limit, token_used, cost_limit_cents, cost_used_cents
- 拦截层：每次 LLM 调用前检查配额；超限抛 402
- 账本页 `/admin/usage`：按用户 / 项目 / 模型 统计

### 备份

- `scripts/backup.sh`：`pg_dump` + `qdrant snapshot` + `redis bgsave` 打 tar → 上传到 S3（可配 endpoint）
- Celery beat daily 调度

## 7. 前端收尾

- 统一设计 tokens（色板 / 间距 / 字号）
- 暗色主题完善
- 移动端：所有核心页面响应式（项目 / 章节 / 设定集 / 知识库）
- i18n：zh-CN / en（所有 key 入 `frontend/src/i18n/`）
- 空态 / 错误态 / loading 统一组件

## 验收标准

- [ ] LangGraph runner 可视化图可打开，老 runner 仍可运行
- [ ] BVSR 一次生成 3 候选，前端能查看并手选
- [ ] 10 个 genre_profiles 种子入库
- [ ] `prometheus_client` /metrics 端点暴露指标，Grafana 看板可打开
- [ ] 镜像 size < 800MB
- [ ] 用户超配额时返回 402 并在 UI 提示
- [ ] CI 绿，main push 触发部署到测试机
- [ ] 移动端可完整读/写一章

## 工作量

约 20-25d（LangGraph 5d / BVSR 3d / 多 Agent 4d / 题材库 3d / Neo4j 集成 3d / 监控+CI 3d / 配额 2d / 前端收尾 3-4d）

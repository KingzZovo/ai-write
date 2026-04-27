# Release Notes — v1.6.0 (Prompt Cache Plumbing + Scene Mode Observability)

**发布日期**：2026-04-27 (Asia/Shanghai)  
**基准**：v1.5.0  
**数据库头**：`a1001900`（无新增迁移，纯运行时 + 观测增强）  
**Git tag**：`v1.6.0`  
**HEAD**：（见 git tag）  
**验收依据**：`docs/v1.5.0-acceptance-report.md` Appendix B + `docs/v1.5.x-v1.6.0-roadmap.md`

## TL;DR

v1.6.0 在 v1.5.0 的 scene + auto-revise + cascade 闭环之上，专注两件事：

1. **Y1+Y2+Y3 Prompt cache plumbing** — Anthropic 的 `cache_control:ephemeral`（system 块超过 4096 字符自动包裹）+ OpenAI 的 `prompt_cache_key`（按 `task_type:model` 聚合）+ Prometheus `llm_cache_token_total{task_type,provider,model,kind=cache_create|cache_read|cache_uncached}` 三视角计数。
2. **X4 Scene mode observability** — 新增三类 Prom 指标：`scene_plan_fallback_total{reason=unparseable|too_few}`、`scene_count_per_chapter` histogram (buckets 1..12)、`scene_revise_round_total{outcome=scored|skipped|revised|timeout|error}`，让 v1.5.0 的 scene_planner / auto-revise 路径首次具备生产可观测性。

配套：**X1 v1.5.0 acceptance close-out**（写入 chapter 5 的完整 e2e SSE + PG truth 到 acceptance report Appendix B）；**Y4 cache_uncached baseline emit**（即使上游代理不返回 `prompt_tokens_details.cached_tokens`，只要传了 `prompt_cache_key` 且 prompt_tokens>0，仍 emit baseline counter，避免 `/metrics` 在不支持 cache 的代理上完全空白）。

## 1. Y1 — Anthropic prompt cache (`cache_control:ephemeral`)

### 行为

- `app/services/model_router.py` `AnthropicProvider.generate / generate_stream`：当 `system` 消息长度 ≥ `ANTHROPIC_CACHE_MIN_CHARS`（默认 4096）且 `ANTHROPIC_PROMPT_CACHE_ENABLED=true`（默认开启）时，把 `system` 由 `str` 提升为 block 列表：
  ```python
  [{"type": "text", "text": system_msg, "cache_control": {"type": "ephemeral"}}]
  ```
  其余短 system 直接透传，零行为变化。

### 环境变量

- `ANTHROPIC_PROMPT_CACHE_ENABLED` (default `true`) — 关闭则全部走旧逻辑。
- `ANTHROPIC_CACHE_MIN_CHARS` (default `4096`) — 低于阈值跳过包装。

### 收据

- `tests/test_v16_prompt_cache.py::test_anthropic_long_system_emits_cache_control`
- `tests/test_v16_prompt_cache.py::test_anthropic_short_system_passthrough`

## 2. Y2 — OpenAI prompt cache key

### 行为

- `OpenAIProvider.generate / generate_stream`：当 `OPENAI_PROMPT_CACHE_ENABLED=true`（默认开启）时，向 `chat.completions.create` 的 `extra_body` 注入：
  ```python
  extra_body["prompt_cache_key"] = f"{task_type}:{model}"
  ```
- `task_type` 来自 `kw.get("task_type", "unknown")`；模型固定的 task 会自然 share 同一 prefix，从而最大化 OpenAI 的 prompt cache 命中。

### 收据

- `tests/test_v16_prompt_cache.py::test_openai_prompt_cache_key_injected`

## 3. Y3 — Cache token Prom counter

### 指标

```
llm_cache_token_total{task_type, provider, model, kind="cache_create"|"cache_read"|"cache_uncached"}
```

- `cache_create` (Anthropic)：`usage.cache_creation_input_tokens`，第一次写入 cache。
- `cache_read` (Anthropic + OpenAI)：Anthropic `usage.cache_read_input_tokens` + OpenAI `usage.prompt_tokens_details.cached_tokens`。
- `cache_uncached`：每次 LLM 调用的 input tokens 减去 cached 部分，是分母（不命中或不支持 cache 时全部计入）。

### 实现

- `app/observability/metrics.py` 注册 `LLM_CACHE_TOKEN_TOTAL`。
- `app/services/model_router.py` `_record_cache_tokens()` helper 安全 best-effort（observability 模块缺失也不抛）。
- AnthropicProvider 与 OpenAIProvider 在拿到 usage 后调用 `_record_cache_tokens`。

### 收据

- `tests/test_v16_prompt_cache.py::test_openai_records_cached_tokens_metric`

## 4. Y4 — Baseline `cache_uncached` emit + e2e smoke

### 问题

部分上游 LLM 代理（如 `141.148.185.96:8317` 透传式代理）：

- 不在 stream chunks 里返回 `usage.prompt_tokens_details.cached_tokens`；
- 甚至 stream 结束后也不补一个带 usage 的最终 chunk（典型表现：`scene_writer` 的 `llm_call_logs.input_tokens=0`）。

这会导致 Y3 的 `llm_cache_token_total` 在 `/metrics` 完全没有任何 sample line（Prometheus client 不会输出从未 inc 的 label 组合），运维侧无法监控。

### 修复

`OpenAIProvider.generate` 末尾的 emit 条件由 `if cached_tokens:` 放宽为：

```python
if _OPENAI_CACHE_ENABLED and usage.input_tokens:
    uncached = max(usage.input_tokens - cached_tokens, 0)
    _record_cache_tokens(task_type, self.name, model, read=cached_tokens, uncached=uncached)
```

这样：
- 上游不返回 `cached_tokens` 时，`cache_read` 仍为 0，**不会**伪造命中；
- `cache_uncached` 永远会随 `prompt_tokens` 累加，给运维 baseline 视图（即使在不支持 cache 的代理后面也能看到生产 token 流）。

### 收据

- 新增 `tests/test_v16_prompt_cache.py::test_openai_baseline_emits_cache_uncached_when_no_cache_field`：构造 `prompt_tokens_details=None` 的 fake usage，断言 `cache_uncached` ≥1500 增长且 `cache_read` 不动。
- 端到端 smoke：chapter 7 (`98d585bd-f590-4ad8-bcad-906b5f73693e`) `use_scene_mode=true, n_scenes_hint=1, target_words=800, auto_revise=false`，5541 字落库；`/metrics` 出现 `llm_cache_token_total{kind="cache_uncached",model="gpt-5.4(high)",provider="openai",task_type="unknown"} 1592.0`。
- 上游代理仍不返回 cache 字段→ `kind="cache_read"` 暂为 0；这是基础设施限制，待换支持 prompt cache 的代理后自动产生命中数据，无需代码改动。

## 5. X4 — Scene mode observability metrics

### 新增 Prom 指标

```
scene_plan_fallback_total{reason="unparseable"|"too_few"}
scene_count_per_chapter (Histogram, buckets [1,2,3,4,5,6,7,8,9,10,11,12])
scene_revise_round_total{outcome="scored"|"skipped"|"revised"|"timeout"|"error"}
```

### 注入点

- `app/services/scene_orchestrator.py`：
  - `_x4_inc_fallback(reason)` — 在两条 fallback 路径（JSON 解析失败、scene 数 <2）调用。
  - `_x4_observe_scene_count(n)` — `plan_scenes()` 拿到最终 brief 列表后调用。
- `app/api/generate.py` revise loop：
  - `_x4_inc_revise(outcome)` — 在 `event:scored` / `event:revise_skipped` / `event:revising` SSE emit 旁边注入。

### 收据

- `tests/test_v16_scene_metrics.py`（3 个用例）：fallback counter / scene_count histogram / revise round 三类各一。
- chapter 6 smoke: `scene_count_per_chapter_count 1.0`、`scene_count_per_chapter_sum 3.0`（完美匹配 `n_scenes_hint=3`）。

## 6. X1 — v1.5.0 acceptance close-out

### 内容

`docs/v1.5.0-acceptance-report.md` 末尾 **Appendix B**：

- chapter 5 (`38a299a8-bf75-4816-ada8-63a2a1ae5ddd`) 完整 SSE 事件链统计：1× generating, 3× saved, 2× evaluating, 3× scored, 2× revising, 1× cascade_triggered, 1× completed。
- PG truth：`chapter_evaluations` 3 行 (overall=7.86 / 8.04 / 7.98，issues 15/18/16)；`cascade_tasks` 1 行 `45d58679-83fe-48f0-a9e2-b25bb0f03186 target=outline severity=critical status=done`；`chapters` ch5 status=completed word_count=9264。
- 30 分钟 `llm_call_logs`：18 scene_writer + 4 scene_planner + 3 evaluation + 2 extraction = 27 调用。
- `docs/v1.5.x-v1.6.0-roadmap.md` 新增 Status 列：X1✅ X4✅ Y1+Y2+Y3✅ Y4✅ Y5✅ X2🟡（v1.7）X3🟡（v1.7）X5⬜（前端，v1.7）。

## 7. 文件清单

| 类别 | 路径 | 摘要 |
|---|---|---|
| 代码 | `backend/app/services/model_router.py` | Y1 cache_control / Y2 prompt_cache_key / Y3 _record_cache_tokens / Y4 baseline emit |
| 代码 | `backend/app/observability/metrics.py` | LLM_CACHE_TOKEN_TOTAL + SCENE_PLAN_FALLBACK_TOTAL + SCENE_COUNT_PER_CHAPTER + SCENE_REVISE_ROUND_TOTAL |
| 代码 | `backend/app/services/scene_orchestrator.py` | X4 _x4_inc_fallback / _x4_observe_scene_count |
| 代码 | `backend/app/api/generate.py` | X4 _x4_inc_revise 在 revise loop 三处 SSE emit 旁 |
| 测试 | `backend/tests/test_v16_prompt_cache.py` | 5 个用例（4 Y1+Y2+Y3 + 1 Y4 baseline）|
| 测试 | `backend/tests/test_v16_scene_metrics.py` | 3 个用例（X4）|
| 文档 | `docs/v1.5.0-acceptance-report.md` | Appendix B (X1 close-out) |
| 文档 | `docs/v1.5.x-v1.6.0-roadmap.md` | Status 列同步 |
| 文档 | `RELEASE_NOTES_v1.6.0.md` | 本文件 |
| 文档 | `CHANGELOG.md` | `## [1.6.0]` 块 |

## 8. 测试矩阵

- `python -m pytest tests/ -q --ignore=tests/integration` → **229 + 1 (Y4 baseline) = 230 passed**
- chapter 6 smoke ✅ (5674 字 / 3 scenes / scene_count_per_chapter histogram populated)
- chapter 7 smoke ✅ (5541 字 / 1 scene / `llm_cache_token_total{kind=cache_uncached}=1592` populated)

## 9. Breaking / 注意

- 无破坏性变更。所有 cache plumbing 由环境变量 gated（默认开启），可设 `false` 退回 v1.5.0 行为。
- `task_type` 在 scene_orchestrator → model_router 链路当前部分调用没传 kwarg（label 显示为 `unknown`）；不影响 metric 正确性，归 v1.7 优化。
- `cache_read` 在不支持 prompt cache 的上游代理后面会一直为 0。这是运维侧的代理选型问题，非代码 bug。

## 10. v1.5.x 遗留 (carry-over)

- **X2** — `knowledge_tasks.py:124` 的 prompt 表 INSERT 在 hot path 的真实源点（早于 v1.5.0 已存在），延后到 v1.7 与 prompt_cache 二期一并清理。
- **X3** — Qdrant orphan slice_ids（`style_samples_redacted` 中存在 PG 已无的 slice），延后到 v1.7 写一个一次性清理脚本。
- **X5** — 前端 cascade 状态面板，待 UI 设计稿后做，归 v1.7。

## 11. 致谢

本版本完全在 King 的 `BDC` + `4-step BDCA` + `自循环自校验自审查` 三轮 directive 下，由后台自治 agent 单线程推进，零中断。

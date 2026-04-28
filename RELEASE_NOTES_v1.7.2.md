# v1.7.2 — Prom LLM 指标全路径覆盖（Z3）

**Tag**: `v1.7.2`｜**HEAD**: `1d00ad8`｜**Base**: `v1.7.1` (`9eaf111`)｜**Date**: 2026-04-28

## TL;DR

v1.7.2 收尾 v1.7.x 观测线工程：把 `time_llm_call` 包裹下沉到 `ModelRouter.{generate, generate_stream, generate_by_route, generate_with_tier_fallback}` 的 8 个 provider 调用点，让所有 LLM 调用——而不仅仅是 `prompt_registry.run_prompt`——都向 Prometheus 发 `llm_call_total` / `llm_call_duration_seconds` / `llm_token_total` 样本。补丁不改外部行为，纯指标修复，零回归（pytest 252 passed，从 v1.7.1 的 248 +4）。

v1.7.x 三连击 (Z1 / Z2 / Z3) 全部交付，剩余 L3（Notion sync 审计）按 King 指示延后到 v1.8 主线。

---

## 1. Z3：ModelRouter `time_llm_call` 全路径覆盖

### 1.1 修复前的问题

v1.7.1 之前，`time_llm_call(task_type, provider, model)` 只在 `app/services/prompt_registry.py:run_prompt` 内层被使用一次，包裹其单一 `router.generate_with_tier_fallback(...)` 调用。这意味着：

- `generate_with_tier_fallback` 的另外 5 个直接调用方（`entity_timeline.py`、`chapter_evaluator.py`、`hook_manager.py`、`consistency_checker.py`、`ooc_checker.py`）从来没有被计入；
- `router.generate` 的 5 个调用方（`knowledge_tasks.py × 2`、`cascade_regenerator.py`、`settings_extractor.py × 2`、`outline_generator.py × 4`）从来没有被计入；
- `router.generate_stream` 的调用方（`outline_generator.py:281`）从来没有被计入；
- `router.generate_by_route` 在 app/ 内当前虽无生产调用方，但路径仍然空跑无指标。

这造成 Prometheus 里的 `llm_call_total{task_type="polishing|extraction|beat_extraction|outline|consistency_llm_check|critic|..."}` 大量缺失，仪表盘只能看到 `prompt_registry`-route 的小子集，调度灰盒。

### 1.2 改动

**File 1: `backend/app/services/model_router.py`**

顶部 `from app.observability.metrics import time_llm_call`。

在以下 4 个方法的 8 个 provider 调用点加 `with time_llm_call(...) as _mbox` 包裹（每个方法的 `_log_meta is None` 直分支与 `as ctx:` 日志分支各一次）：

| 方法 | 直分支 | 日志分支 | 包裹粒度 |
|---|---|---|---|
| `generate` (L622-657) | L634 | L655 | `result = await provider.generate(...)` + `_mbox["input_tokens"]/...` |
| `generate_stream` (L659-693) | L677 | L696 | 整个 `async for chunk in provider.generate_stream(...): yield chunk` 循环 |
| `generate_by_route` (L694-732) | L717 | L739 | 同 `generate` |
| `generate_with_tier_fallback` (L900-960) | L941 | L961 | **在 `for ... attempts` 循环 INSIDE**，每次重试 attempt 独立观测一次 |

关键设计点：

- **provider label**: 用 `provider.__class__.__name__`（`OpenAIProvider`/`AnthropicProvider`/`OpenAICompatProxy`），稳定低基数，不用 `route.provider_key`（端点 UUID 高基数会污染 Prom 时间序列）。
- **token 填充**: `GenerationResult` 返回的方法在 with-block 内填 `_mbox["input_tokens"] = result.usage.input_tokens; _mbox["output_tokens"] = result.usage.output_tokens`。流式方法因为没有最终 usage 暂留 0（v1.8 可补流末 usage 解析）。
- **status=error**: `time_llm_call` 的 `try/except BaseException` 已经在异常路径上把 `box["status"]="error"` 然后 re-raise。所以失败的 fallback attempt 会自然记 `status="error"`，下一个 attempt 成功记 `status="ok"`，无需额外编排。

**File 2: `backend/app/services/prompt_registry.py:720-755`**

移除外层 `with time_llm_call(task_type, _provider, _model) as mbox:`（被内层 wrap 取代，否则双计）。改为单层 `result = await router.generate_with_tier_fallback(...)`。原本基于 `route.provider` 的字符串作为 provider label 也一起退役——用 `provider.__class__.__name__` 后保持全栈一致。

### 1.3 测试

新增 `backend/tests/test_v17_z3_time_llm_call_propagation.py`（4 个 case）：

1. `test_generate_emits_llm_call_total_with_real_task_type`：fake provider 返回固定 `TokenUsage(input=11, output=7, total=18)`，断言 `LLM_CALL_TOTAL{task_type="z3_test_task",status="ok"} +1`、`LLM_TOKEN_TOTAL{direction="input"} +11`、`LLM_TOKEN_TOTAL{direction="output"} +7`。
2. `test_generate_by_route_emits_llm_call_total_with_default_by_route`：合成最小 route 对象走 `generate_by_route`，断言 `task_type="by_route"` 的 `+1`。
3. `test_generate_stream_emits_llm_call_total`：异步迭代 fake stream，断言 `+1`（token 仍 0 是预期）。
4. `test_generate_records_error_status_on_provider_exception`：fake provider raise，断言 `status="error"` 计数 +1 且异常被 re-raise。

断言读样本用 `REGISTRY.get_sample_value("llm_call_total", labels)` —— 因为 `app/observability/metrics.py` 的所有 LLM Counter/Histogram 都用 `registry=REGISTRY` 注册到自定义 `CollectorRegistry(auto_describe=True)`。

### 1.4 验证

- `python3 -c 'import ast; ...'` 两文件 syntax 通过。
- `pytest tests/ -q --ignore=tests/integration`：**252 passed in 5.26s**（v1.7.1 baseline 248 → +4 新增）。
- `docker cp` 到 `ai-write-backend-1` + `ai-write-celery-worker-1` 后 `docker restart`，`/api/health=200`。
- 与 Z1 的契合：`task_type` 标签由 Z1 propagation 提供，Z3 的 wrap 直接消费它（不再有 `task_type="unknown"`）。

---

## 2. v1.7.x 总览（Z1 / Z2 / Z3 全部交付）

| 项 | 范围 | 状态 | Tag/Commit |
|---|---|---|---|
| **v1.7.0** (Z0 / X 系列收尾) | scene_mode、cascade panel API | ✅ | `v1.7.0` → `60e7e95` |
| **v1.7.1 Z1** | `task_type` 透传到 provider call kwargs | ✅ | `89bdaaf` |
| **v1.7.1 Z2** | `CascadeTasksPanel` 桌面工作区挂载 + 详情弹窗 | ✅ | `e5df3ac` |
| **v1.7.1** docs | RELEASE_NOTES + CHANGELOG | ✅ | `9eaf111` / `v1.7.1` |
| **v1.7.2 Z3** | `ModelRouter.generate*` 全路径 `time_llm_call` 包裹 | ✅ | `1d00ad8` |
| **L3** Notion sync 审计 | — | ⬜ 延后 v1.8 | — |

---

## 3. 测试与回归

- pytest（不含 integration）：**252 passed, 8 warnings, 5.26s**
- frontend `tsc --noEmit`：clean（v1.7.1 已验证，本版本未改前端）
- `/api/health`：200
- worker 日志（24h 滚动）：0 条 "attached to a different loop"
- alembic head：`a1001900`（无新迁移）

---

## 4. Breaking changes

**无**。完全是观测层加固，不改 API、Schema、外部行为。

唯一可能的指标层差异：v1.7.1 之前用 `route.provider`（字符串如 `"openai_compat_proxy"`）作为 `provider` 标签；v1.7.2 改为 `provider.__class__.__name__`（如 `"OpenAICompatProxy"`）。如果你已经有依赖该字段精确字符串的 PromQL 告警/面板，需要做一次正则更新。建议做法：

```promql
# 旧
sum by (provider) (rate(llm_call_total{provider="openai_compat_proxy"}[5m]))
# 新
sum by (provider) (rate(llm_call_total{provider=~".*Proxy|.*Provider"}[5m]))
```

---

## 5. 未完事项 / 后续

- **L3 — Notion sync 审计**：King 指示延后。下一窗口启动 v1.8 时再视情况安排。
- **流式 token usage 回填**：`generate_stream` / `stream_by_route` / `stream_with_tier_fallback` 当前在 `time_llm_call` 内 `_mbox["input_tokens"] = 0`。如果 provider 在 stream end-of-stream 帧带 usage，可在 v1.8 解析后回填。
- **`stream_by_route` / `stream_with_tier_fallback` 包裹**：本版本只覆盖了 King 列出的 4 个方法。两个 stream 兄弟方法目前没有 app/ 内的生产调用方，但为对称性可在 v1.8 一并处理。

---

## 6. Git

```
1d00ad8 fix(v1.7.2 Z3): wrap ModelRouter.generate* with time_llm_call
9eaf111 docs(v1.7.1): release notes + CHANGELOG block
e5df3ac feat(frontend): v1.7.1 Z2 mount CascadeTasksPanel + add detail modal
89bdaaf fix(v1.7.1 Z1): propagate task_type into provider call kwargs
60e7e95 (tag: v1.7.0)
```

**Tag**: `v1.7.2` (annotated) → `1d00ad8`

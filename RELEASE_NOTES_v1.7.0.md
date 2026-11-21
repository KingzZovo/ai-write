# Release Notes — v1.7.0 (Carry-forward Hardening + Cascade Tasks UI)

**发布日期**：2026-04-28 (Asia/Shanghai)  
**基准**：v1.6.0  
**数据库头**：`a1001900`（无新增迁移，纯运行时硬化 + 工具/UI 增强）  
**Git tag**：`v1.7.0`  
**HEAD**：（见 git tag）  
**承接**：`docs/v1.5.x-v1.6.0-roadmap.md` X2/X3/X5

## TL;DR

v1.7.0 是 v1.6.0 之后的一次 carry-forward 收尾，把 v1.5.x → v1.6.0 roadmap 上推迟的三项 X 序列工作全部落地：

1. **X2 — `_run_async` 路径统一** — `app/tasks/knowledge_tasks.py` 与 `app/tasks/style_tasks.py` 中各自的 `_run_async()` helper 改为 thin delegator，统一走 `app/tasks/__init__.py:_run_async_safe`（先 `reset_engine()` + `reset_model_router()`，结束后 `dispose_current_engine_async()`），从根上闭掉 "attached to a different loop" 警告类的回归路径。
2. **X3 — Qdrant 孤立 slice 清理** — 新 `scripts/cleanup_orphan_qdrant_slices.py`（PG-as-truth 对账，`--dry-run` 默认，`--apply` 批量删除）。一次性回收 `style_samples_redacted` 8280 条孤立点（12393 → 4113，与 PG `reference_book_slices` 4115 对齐）；`beat_sheets` / `style_profiles` 已分别为 4115/4115，干净。
3. **X5 — cascade_tasks UI 面板** — 新 `GET /api/projects/{pid}/cascade-tasks`（列表 + `chapter_id` / `status` 过滤）+ `/summary`（按 status 计数）+ `/{tid}` 详情，前端新 `CascadeTasksPanel.tsx` + 独立 `/cascade-tasks?project_id=...` 路由，自动 15 s 轮询活跃行。

所有三项都基于 v1.5.0 的 cascade + auto-revise + scene 闭环之上零破坏性硬化，运行时行为不变。

## 1. X2 — Unified `_run_async` helpers in worker tasks

### 行为

- 真源已在 v1.13 的 `app/db/session.py` 中修好（callable proxy + `_State` + `reset_engine()` + `dispose_current_engine_async()`），但仍有两个 worker 任务模块各自维护本地 `_run_async()`：
  - `app/tasks/knowledge_tasks.py` (8 个 call sites at L53/115/433/538/661/744/854/944)
  - `app/tasks/style_tasks.py` (1 个 call site at L27)
- 现把这两处的 `_run_async()` 改为：

  ```python
  def _run_async(coro):
      from app.tasks import _run_async_safe
      return _run_async_safe(coro)
  ```

  统一走 `_run_async_safe(coro)`：进入前 `reset_engine()` + `reset_model_router()`，结束 finally 里 `dispose_current_engine_async()` + `loop.close()`。

### 影响

- 24 h worker 日志里 "attached to a different loop" 警告 = **0**。X2 是行为零变更的 hardening，将 9 个 call site 全部纳入统一 hygiene。
- 测试：`tests/test_v17_x2_unified_run_async.py` 4 用例（delegation × 2 + reset 顺序 + callable-proxy 契约）。

## 2. X3 — Qdrant orphan slice cleanup

### 行为

- `scripts/cleanup_orphan_qdrant_slices.py`：
  - 滚动扫描指定 collection（`--collection` 可重复，默认仅 `style_samples_redacted`），分页 512。
  - 纯函数 `compute_orphan_ids(qdrant_points, pg_slice_ids) -> (orphan_ids, orphan_count, kept_count)`：orphan = `payload['slice_id'] is None or sid not in pg_truth`。
  - PG truth：`SELECT id::text FROM reference_book_slices`（4115 行）。
  - `--dry-run` 默认（只打印 orphan 计数），`--apply` 后按 `PointIdsList` 批量 200 删除。
- 之所以孤立：早期 ingest 没有走 `_deterministic_id`，多次 retry 同一 (book, slice) 会写出多 point；v1.5.0 起 `app/services/qdrant_store.py` 已经全部用 `_deterministic_id(f"style_sample_{book_id}_{slice_id}")` / `beat_{...}` / `style_profile_{...}` / `plot_{...}` / `style_{...}` 写入，新增数据不会再产生孤儿。

### 真数据回收

- `style_samples_redacted` 12393 → 4113（删 8280，与 PG=4115 相差 2，为未进入 redacted 通道的少数 slice，可接受）。
- `beat_sheets` 4115/4115、`style_profiles` 4115/4115 干净，无操作。
- 二次 dry-run 确认 `orphan=0` 全部三 collection（脚本幂等）。

### 测试

- `tests/test_v17_x3_orphan_cleanup.py` 5 用例（orphan / kept / missing payload / empty PG / string-id），都覆盖 `compute_orphan_ids` 纯逻辑。

## 3. X5 — cascade_tasks read-only API + frontend status panel

### 后端

- 新 `app/api/cascade.py`，路由 prefix `/api/projects/{project_id}/cascade-tasks`：
  - `GET ""` → `list[CascadeTaskResponse]`，可选 `chapter_id` / `status` 过滤，`limit ∈ [1,500]` 默认 100，按 `created_at desc`。
  - `GET "/summary"` → `{pending,running,done,failed,skipped,total}`。
  - `GET "/{task_id}"` → 单条详情，project_id 不匹配返 404。
  - 非法 `status` 返 400，列表中 `status` 校验白名单：`pending | running | done | failed | skipped`。
- `app/main.py` 在 `evaluate_api` 之后 `include_router(cascade_api.router)`。
- 纯只读：cascade 行仍由 `app/tasks/cascade.py` 的 planner 写入。

### 前端 (Next.js App Router)

- `frontend/src/components/panels/CascadeTasksPanel.tsx`：
  - 状态标签（5 色）+ severity 标签（high/critical）+ summary chips。
  - 状态过滤下拉、手动 Refresh、活跃行（pending/running）存在时 15 s 自动轮询。
  - 错误 banner、空态文案、issue_summary 行级 clamp。
- `frontend/src/app/cascade-tasks/page.tsx`：独立路由 `/cascade-tasks?project_id=<uuid>[&chapter_id=<uuid>]`，Suspense 包裹 useSearchParams 满足静态预渲染。
- `tsc --noEmit` 干净通过。

### 真数据 smoke

- `GET /api/projects/f14712d6-.../cascade-tasks?limit=5` 返回 v1.5.0 C4-7 烟测时落库的那条 `45d58679-...`（target_entity_type=outline, severity=critical, status=done, attempt_count=1）。
- summary 返回 `{done: 1, total: 1}`。

### 测试

- `tests/test_v17_x5_cascade_api.py` 6 用例：from_row mapper 单测 × 2 + ASGITransport 集成（空列表、全零 summary、非法 status 400、未知 id 404）。

## 4. Schema

- 无新增迁移。`alembic head=a1001900` 与 v1.5.0/v1.6.0 相同。

## 5. 测试 / 回归

- pytest **245 passed**（v1.6.0 230 + X2 4 + X3 5 + X5 6 = 245）。
- worker 24 h "attached to a different loop" 警告 = 0。
- frontend `tsc --noEmit` 干净。

## 6. Breaking / 注意

- 无破坏性变更。X2 是 helper 收编，行为完全等价。
- X3 cleanup 已对 `style_samples_redacted` 执行 `--apply`（生产容器内）；二次 dry-run 已确认幂等，再跑无作用。
- X5 endpoint 是 read-only；cascade 行写入路径仍走 planner。前端 `/cascade-tasks` 路由独立，不耦合 `/workspace`。
- `task_type="unknown"` Prom label 仍未补（计入 v1.7 未尽事项 → v1.8 候选）。

## 7. Carry-forward 状态

| ID | 标题 | v1.7.0 状态 |
| -- | -- | -- |
| X1 | v1.5.0 acceptance close-out | ✅ v1.6.0 |
| X2 | `_run_async` 统一 | ✅ v1.7.0 commit `7975183` |
| X3 | Qdrant 孤立 slice cleanup | ✅ v1.7.0 commit `819a4cf` |
| X4 | Scene mode observability | ✅ v1.6.0 |
| X5 | cascade_tasks UI 面板 | ✅ v1.7.0 commit `85111c2` |
| Y1+Y2+Y3 | Prompt cache plumbing | ✅ v1.6.0 |
| Y4+Y5 | Baseline + release tag | ✅ v1.6.0 |

v1.5.x → v1.7.0 carry-forward 全部交付。后续待办：L3（Notion 同步审计 / 非软件路径），按 King 指示推迟。

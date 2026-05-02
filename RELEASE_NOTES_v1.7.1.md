# Release Notes — v1.7.1 (task_type Prom 修复 + Cascade 任务面板内嵌)

**发布日期**：2026-04-28 (Asia/Shanghai)  
**基准**：v1.7.0  
**数据库头**：`a1001900`（无新增迁移，纯运行时修复 + UI 集成）  
**Git tag**：`v1.7.1`  
**承接**：v1.7.0 RELEASE_NOTES 末尾指出的两项遗留项

## TL;DR

v1.7.1 是一个轻量点状补征 release，主要点名两件事：

1. **Z1 — `task_type` Prometheus label 修复** — `LLM_CACHE_TOKEN_TOTAL` 以及下游 cache 指标在 v1.7.0 之前，只要调用路径进入 `ModelRouter.generate_by_route`，task_type label 都会在 emit 点退化为 `"unknown"`。根因：`generate_by_route` 未接收 `task_type`，也未将其作为 `**kw` 中的一项传给 provider 底层调用，后续 `_record_cache_tokens` `kw.get("task_type", "unknown")` 于是总是拿到 fallback。v1.7.1 在 `app/services/model_router.py` 中：
   - 给 `generate_by_route` 的签名加 `task_type: str = "by_route"` 默认参数。
   - `generate` / `generate_stream` / `generate_by_route` / `generate_with_tier_fallback` 共 4 个路径、合计 12 个 provider 调用点，全部从 `temperature=eff_temp, max_tokens=eff_max, **kw)` 提升为 `temperature=eff_temp, max_tokens=eff_max, task_type=task_type, **kw)`。
   - 缓存 emit 点保留 `task_type or "unknown"` 防御性 fallback，但现在上游会总是传递真实值，不会再看到 unknown。
   - 新增 3 个单元测试（`tests/test_v17_z1_task_type_propagation.py`）： `generate` / `generate_stream` propagation + `generate_by_route` 签名默认。
2. **Z2 — Cascade 任务面板内嵌到 `/workspace`** — v1.7.0 X5 只交付了独立路由 `/cascade-tasks?project_id=...`，King 的使用路径未在主工作区联动。v1.7.1 中：
   - `frontend/src/components/workspace/DesktopWorkspace.tsx` 新增 `CascadeTasksPanel` 的 `dynamic()` 懒加载导入，以 `<CollapsibleSection title="Cascade 任务">` 挂载在“版本历史”之后、drawer 区之前，门控 `currentProject`，传入 `projectId={currentProject.id}`，`chapterId={selectedChapterId || undefined}`。选中某章节时面板自动 scope 到该章节，未选时呈现项目级货币列表。
   - `frontend/src/components/panels/CascadeTasksPanel.tsx` 新增详情 modal `CascadeTaskDetailModal`：点击任一行 → modal 反查 `GET /api/projects/{pid}/cascade-tasks/{tid}`，展示完整 `target_entity_type/id`、`source_chapter_id`、`source_evaluation_id`、`parent_task_id`、`attempt_count`、`created_at` / `started_at` / `completed_at` 时间轴，计算 duration（`completed_at - started_at` 或 `now - started_at`），独立呈现 `issue_summary` 与 `error_message`。点击遮罩或按 `Esc` 关闭。
   - 被点击的行高亮、鼠标 hover 有 `cursor-pointer` + `bg-blue-50/40` 类，`title` 提示“Click for detail”。

## 1. Z1 — task_type 贯穿

### 出现

- v1.6.0 Y3 引入 `LLM_CACHE_TOKEN_TOTAL{task_type, provider, model, kind}`；v1.6.0 Y4 增加 baseline `cache_uncached` emit。两者都在 OpenAI/Anthropic provider 的 `_record_cache_tokens(...)` 路径里读 `kw.get("task_type")`。
- 实际产生 Counter 时，`task_type` 标签几乎总是为 `"unknown"`。护火墙外看到的使用主要是`unknown / openai_compat_proxy / gpt-5.2 / cache_uncached`。

### 根因

- `ModelRouter.generate_by_route(route, prompt, **kw)`（位于 `app/services/model_router.py:694-728` 左右）在外部调用点（knowledge_tasks / chapter_generator / scene_planner 等）调用时会提供 `task_type="polishing"` / `"generation"` / `"extraction"` 等字段在 `**kw`，但 `generate_by_route` 内部选中路由后调用 `provider.generate(prompt, model=..., temperature=..., max_tokens=..., **kw)` 的结构里，**`task_type` 未被重新作为独立参数传递**，只要上游计算的 `kw` 装载不包含它，后续 `_record_cache_tokens` 就取不到。
- 同样问题在 `generate` / `generate_stream` / `generate_with_tier_fallback` 下的调用点也存在，总计 12 处。

### 修复

- `model_router.py`：
  - L698 提升签名：
    ```python
    async def generate_by_route(
        self,
        route: str,
        prompt: ChatPrompt | str,
        *,
        task_type: str = "by_route",
        **kw,
    ) -> str:
    ```
  - 12 个 provider 调用站点（行 634 / 652 / 671 / 689 / 709 / 728 / 749 / 768 / 927 / 944 / 1010 / 1028）从
    ```python
    return await provider.generate(
        prompt, model=eff_model, temperature=eff_temp, max_tokens=eff_max, **kw
    )
    ```
    改为
    ```python
    return await provider.generate(
        prompt, model=eff_model, temperature=eff_temp, max_tokens=eff_max,
        task_type=task_type, **kw
    )
    ```
    `generate_stream` 调用站点同理。
- 防御性 fallback：Provider 内部 `_record_cache_tokens` 仍写 `task_type or "unknown"`，不变。现在现实上会总是拿到上游推下来的真实值（polishing/generation/extraction/beat_extraction/outline/evaluation/redaction/summary/consistency_llm_check/critic/compact 等 + 默认 `by_route`）。

### 测试

- `tests/test_v17_z1_task_type_propagation.py` (3 用例： generate / generate_stream propagation + generate_by_route 签名默认)。
- pytest **248 passed** (v1.7.0 245 + Z1 3)。
- 生产容器已 `docker cp` 同步、`/api/health=200`。
- commit：`89bdaaf`。

## 2. Z2 — Cascade 任务面板内嵌 + 详情 modal

### Workspace 集成

- `DesktopWorkspace.tsx`：
  - L22 新增懒加载导入：
    ```ts
    const CascadeTasksPanel = dynamic(
      () => import('@/components/panels/CascadeTasksPanel').then(m => ({ default: m.CascadeTasksPanel })),
      { ssr: false },
    )
    ```
  - L1248-1257 新增面板 section（在“版本历史”之后、drawer 区之前）：
    ```tsx
    {currentProject && (
      <CollapsibleSection title="Cascade 任务">
        <div className="px-4">
          <CascadeTasksPanel
            projectId={currentProject.id}
            chapterId={selectedChapterId || undefined}
          />
        </div>
      </CollapsibleSection>
    )}
    ```

### 详情 modal

- `CascadeTasksPanel.tsx`：
  - 表格行增加 `cursor-pointer` + `onClick={() => setOpenTask(r)}`。
  - `CascadeTaskDetailModal` 在 mount 后反查 `/api/projects/{pid}/cascade-tasks/{tid}` 拿最新状态，字段网格呈现全量信息，`issue_summary` 和 `error_message` 不被截断、允许换行。
  - 事件绑定：点击遮罩 → close；`Esc` 键 → close。
  - 点击 modal 内容 `stopPropagation`。

### 验证

- `npx --no-install tsc --noEmit` 干净。
- commit：`e5df3ac`。

## 3. Schema

- 无新增迁移。`alembic head=a1001900` 与 v1.7.0 相同。

## 4. 测试 / 回归

- pytest **248 passed**（v1.7.0 245 + Z1 3 = 248）。
- frontend `tsc --noEmit` 干净。
- worker 24 h “attached to a different loop” 警告 = 0（v1.7.0 X2 后保持 0）。
- 真数据：project `f14712d6` 上 `cascade-tasks/summary` 仍 `{done: 1, total: 1}`；workspace 面板选中 chapter 5 时能看到该货币、点开详情呈现 outline / critical / done。

## 5. Breaking / 注意

- 无破坏性变更。
- Z1 有一点需注意： `provider.generate(...)` 现在会多拿到一个 `task_type=` kwarg。所有 `BaseProvider` 实现（OpenAIProvider / AnthropicProvider / OpenAICompatProxy）本来就接受 `**kwargs` 以走到 `_record_cache_tokens(kw=...)`，因此二进制兼容。
- Z2 中 `CascadeTasksPanel` 仍可独立作为 `/cascade-tasks?project_id=...` 路由使用，不要求仅限 workspace 内嵌。

## 6. 未完事项 → v1.8 候选

- **Z3 — `time_llm_call` 覆盖增强**：当前 `LLM_CALL_TOTAL` / `LLM_TOKEN_TOTAL` / `LLM_CALL_DURATION` 仅在 `app/services/prompt_registry.py:736` 点发出。走 `ModelRouter.generate*` 的路径（knowledge_tasks / chapter_generator / scene_planner 等）未包裹 `time_llm_call`，导致它们仍不走到这三个 Counter/Histogram。Z3 需为 `ModelRouter.generate / generate_by_route / generate_with_tier_fallback` 增加 emit。
- **L3 — Notion 同步审计**：依然推迟。
- **task_type 默认值 `"by_route"`**：仅在 上游未传入 task_type 时会出现。当前代码调用者大多会传 `task_type="polishing" / "generation" / ...`，但仍有少量调用点只传 `route`。Z3 后可考虑在那些点反推上游补上语义 task_type。

## 7. v1.7.x 进度表

| ID | 标题 | 状态 | commit |
| -- | -- | -- | -- |
| Z1 | task_type Prom 修复 | ✅ | `89bdaaf` |
| Z2 | Cascade 面板内嵌 + 详情 modal | ✅ | `e5df3ac` |
| Z3 | time_llm_call 覆盖 | ⬜ v1.8 候选 | — |
| L3 | Notion 同步审计 | ⬜ 推迟 | — |

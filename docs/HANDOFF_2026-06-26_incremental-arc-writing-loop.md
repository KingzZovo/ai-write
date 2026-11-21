# HANDOFF — 子项目 A：弧式增量创作循环（Incremental Arc Writing Loop）

- 日期：2026-06-26
- 分支：`rescue/2026-05-17-baseline`
- 状态：**已实现、已部署 live、685 pytest 全绿**
- 设计：`docs/superpowers/specs/2026-06-26-incremental-arc-writing-loop-design.md`
- 计划：`docs/superpowers/plans/2026-06-26-incremental-arc-writing-loop.md`（9 个 TDD 任务，全部完成）

---

## 1. 这是什么 / 为什么

用户的核心创作哲学：作者很少能在创作初期就把几千章大纲、伏笔、悬念一次性想明白。真实创作是**从一个点子出发，一段一段（一个"弧"≈20 章）地写**，在过程中丰富内容，绝不预先规划几百几千章的大伏笔。

旧流程（书→卷→章全量 750 章大纲→批量生成）违背这一点。子项目 A 提供**相反哲学**的并行编排路径：

```
点子 + 背景设定 + 开场场景
  └─ （可选）kickoff 问题补全设定
  └─ 生成「一段连贯故事」的小弧大纲（≈20 章，显式禁止千章伏笔）
  └─ 写第 1 章（复用子项目 B 的 run_chapter_pipeline）→ 停，awaiting_direction
  └─ 作者给下一步方向 → 写第 2 章 … 直到写满 target_chapters
  └─ 弧 completed：给 N 个下一弧建议 + 问下一弧开场
  └─ 进入下一弧（volume_idx+1），循环
```

---

## 2. 关键设计决策（先前会话确认）

- **渐进式成为新默认**，旧全量大纲流程降级保留（不删）。
- **弧物理复用 `Volume` 表**（一弧 = 一卷，`volume_idx` = 弧序号），**零迁移**。弧状态寄存在 volume-level `Outline.content_json._arc` 命名空间。
- **A 复用 B**：A 只负责"弧编排 + 下一章 brief"，章节正文生成仍走既有 `/api/generate/chapter`（内部已是 `run_chapter_pipeline`）。
- **不动旧 `outline_generator`**：A 是独立新服务（相反哲学，不改旧的）。

---

## 3. 落地的文件

| 文件 | 职责 | 新建/改动 |
|------|------|-----------|
| `backend/app/services/arc_loop.py` | ArcState 解析/序列化、`advance_arc_state` 纯状态机、`generate_arc_outline`（反长线哲学约束 + 降级）、kickoff 问题、弧末建议、下一章 brief | 新建 |
| `backend/app/api/arc.py` | 7 个端点（见下）| 新建 |
| `backend/app/main.py` | 注册 arc 路由 | 改 2 行 |
| `backend/app/services/prompt_registry.py` | `arc_outline→outline_volume`、`arc_kickoff→critic`、`arc_suggest→critic` fallback | 改 3 行 |
| `backend/tests/services/test_arc_loop.py` | arc_loop 单元（12 例）| 新建 |
| `backend/tests/api/test_arc_api.py` | arc API 契约（6 例）| 新建 |

### 弧状态结构（`Outline.content_json`）

```json
{
  "volume_idx": 1,
  "beats": [{"chapter": 1, "beat": "..."}],
  "_arc": {
    "is_arc": true,
    "title": "边境小城御敌",
    "core_setup": "...", "opening_scene": "...",
    "target_chapters": 20, "status": "active",
    "chapters_written": 0, "running_outline": "",
    "next_direction": null, "suggestions": []
  }
}
```

`status`：`active`（可写）｜`awaiting_direction`（等作者方向）｜`completed`（弧满）。
`parse_arc_state` 对无 `_arc` 的旧 volume 返回 `None` → API 对旧项目返回 `{"arc": null}`，前端回退旧 wizard。

---

## 4. API 端点（`/api/arc`，已 live 验证全部注册）

| 方法 | 路径 | 作用 |
|------|------|------|
| POST | `/api/arc/{pid}/start` | 用点子+背景+开场建第一弧（建 Volume + Outline(_arc)，大纲失败 502 回滚不建半截） |
| GET | `/api/arc/{pid}/current` | 返回当前最高 volume_idx 的弧状态；无弧 → `{"arc": null}` |
| POST | `/api/arc/{pid}/chapter-written` | 章节写完后推进状态（chapters_written+1，更新 running_outline，写满→completed 否则→awaiting_direction） |
| POST | `/api/arc/{pid}/next-direction` | 作者提交下一章方向，status→active |
| GET | `/api/arc/{pid}/chapter-brief` | 组装下一章 brief（弧标题+故事线+作者方向+本章 beat），喂给 `/api/generate/chapter` |
| POST | `/api/arc/{pid}/complete` | 标 completed + 生成下一弧建议 |
| POST | `/api/arc/{pid}/next-arc` | 建 volume_idx+1 新弧（要求当前弧 completed，否则 409） |

---

## 5. 降级与边界

- kickoff 问题/弧末建议 LLM 失败 → 返回 `[]`（跳过，不阻断）。
- 弧大纲生成失败 → `{"available": False}` → API 502，不建半截 Volume。
- `target_chapters` 缺省 20，夹紧 **4–40**（注意：写满测试至少要 4 章）。
- `next-arc` 前当前弧必须 `completed`，否则 409。
- task_type `arc_outline`/`arc_kickoff`/`arc_suggest` 未配 PromptAsset 时走 fallback（`outline_volume`/`critic`），开箱即用。

---

## 6. 验证

- **685 pytest 全绿**（666 子项目 B 后基线 + 19 新 arc 测试）。
- `docker compose up -d --build backend` 已部署；live `/api/health` 200；OpenAPI 列出全部 7 个 arc 路由；`/api/arc/{pid}/current` 对非弧项目实测返回 `{"arc": null}`。
- 全程 mock LLM，无真 LLM/真库依赖（API 测试打 dev 库并清理）。

---

## 7. 仍待办（非阻塞，后续可做）

1. **前端接入**：本轮只做后端 API（本环境无法有效测前端）。前端需按既有 wizard 模式接 7 个端点：创世输入 → kickoff 问答（复用 `AskUserPrompt`）→ 逐章「写一章/给方向」循环 → 弧末建议卡。
2. **章节写完自动回调 `/chapter-written`**：目前 `/chapter-written` 需显式调用来推进弧状态。可在 `/api/generate/chapter` 完成后、当项目处于 arc 模式时自动 POST，串成闭环（本轮未做，避免改动 B 的生成端）。
3. **`arc_*` 专用 PromptAsset**：当前走 fallback，可在 `/prompts` 配专用 prompt/model 调优弧大纲与建议质量。
4. **kickoff 答案回填**：`build_arc_kickoff_questions` 产问题，但把答案合并回 `core_setup`/`background` 的逻辑留给前端编排（API 已支持在 `start` 时传入完整字段）。

---

## 8. 与子项目 B 的关系

A 的「写一章」直接复用 B 的 `run_chapter_pipeline`（drafter→logic_critic→prose_polish）。`/chapter-brief` 产出的 brief 作为 `chapter_outline` 喂给 `/api/generate/chapter`，正文生成 + 三角色质量管线全部沿用 B，A 不重复实现任何生成逻辑。

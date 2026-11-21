# 子项目 A：弧式增量创作循环（Incremental Arc Writing Loop）

- 状态：设计已确认（核心决策在先前会话敲定），直接进入实现
- 日期：2026-06-26
- 分支：`rescue/2026-05-17-baseline`
- 关联：`/goal` 复合目标的第三个子项目（B 先于 A 已完成）。A 复用 B 的 `run_chapter_pipeline`。

## 背景与动机（用户原话提炼）

现有所有小说工具（含本项目旧流程）都要求作者**上来就写完整大纲**，一次性产出几百几千章的大纲。用户认为这违背真实创作：

> 很少有作者在创作初期就能把几千章的大纲、引子钩子、剧情悬疑、核心内容一次性想明白。一般都是先有点子，然后由此产生大致想法，在不断创作的过程中才能丰富内容。一次性产出太多，连调整都不好调整。

用户要的流程：

1. 从一个**点子**出发；
2. 作者提供一个**自洽的背景设定** + 一个**开场章节的情节**；
3. 系统**问几个问题**补全初始设定；
4. 然后**一章一章创作**，背后大纲只控制**一段连贯的章节故事**（一个"弧"，≈20 章），**不考虑后面几百几千章的大伏笔**；
5. 写完第一章**停下**，作者说"主角此时发现跑不了，于是打算狐假虎威 xxx" → 据此写第二章；
6. 一直到这一弧（如"边境小城御敌"）结束（≈20 章）；
7. 弧结束后**根据背景设定给几个建议**，并**要求作者提供下一段场景**大概如何、怎么开场、什么契机；
8. 进入下一弧，循环。

例：写玄幻，主角出生边境小城。初始只提供世界设定/功法/战力体系 + 一个核心设定（主角在边境小城有什么大敌、当前状况）+ 当前场景（xxx 上门找茬，主角刚穿越打算先出门看情况、不行就跑）。

## 关键决策（先前会话已确认）

- **渐进式成为新默认**，旧的"全书 750 章大纲→批量生成"流程降级保留。
- **弧取代卷**成为二级结构。实现上：**物理复用 `Volume` 表**（一弧 = 一卷，`volume_idx` = 弧序号），语义重新诠释为"弧"——增量创建（写一弧才建下一弧），而非一次性铺 5 卷×150 章。
- **A 复用 B**：A 的"写一章"那一步直接调用 B 的 `run_chapter_pipeline()`（已上线）。
- **不破坏现有数据/迁移**：神裔现有 750 章卷结构不动；A 是并行的新编排路径，靠 arc 标记区分。

## 架构总览

A 的新增**只有编排/状态机层**，几乎不碰数据模型：

```
点子 + 背景设定 + 开场场景
   └─ build_arc_kickoff_questions() → 几个补全设定的问题（复用 AskUserPause 落库）
         ↓ 作者回答
   └─ generate_arc_outline() → 生成「一段连贯故事」的小弧大纲（≈20 章，显式禁止千章伏笔）
         ↓ 建 Volume(arc) + volume-level Outline(content_json._arc = 弧状态)
   └─ 写第 1 章（复用 run_chapter_pipeline）→ 停，status=awaiting_direction
         ↓ 作者给下一步方向
   └─ 写第 2 章 … 直到 chapters_written 达到 target_chapters
   └─ 弧结束：build_arc_completion_suggestions() 给建议 + 问下一弧开场
         ↓ 作者提供下一弧场景
   └─ 进入下一弧（volume_idx + 1），循环
```

### 组件与职责

| 组件 | 文件 | 职责 | 依赖 |
|------|------|------|------|
| 弧状态机 + 大纲 | `services/arc_loop.py`（新） | 弧状态解析/遷移、kickoff 问题、小弧大纲生成、下一章 brief、弧末建议 | run_structured_prompt、run_text_prompt |
| 弧 API | `api/arc.py`（新） | start-arc / next-chapter-brief / state / complete-arc / start-next-arc 端点 | arc_loop、Volume/Chapter/Outline、run_chapter_pipeline、AskUserPause |
| 章节生成 | `services/chapter_pipeline.run_chapter_pipeline`（既有，B） | 写一章（三角色管线） | 原样 |
| Q&A | `AskUserPause` + `/api/ask-user`（既有） | 补全设定问答 | 原样 |
| 容器 | `Volume` / `Chapter` / `Outline`（既有） | 弧=卷、章、弧状态 sidecar | 原样 |

### 弧状态结构（存于 volume-level `Outline.content_json._arc`）

复用既有 `Outline(level="volume")` 行，`content_json` 加 `_arc` 命名空间，**无需迁移**（`level` 是自由字符串，`content_json` 是 JSON）：

```json
{
  "volume_idx": 1,
  "_arc": {
    "is_arc": true,
    "title": "边境小城御敌",
    "core_setup": "主角在边境小城的大敌与当前状况（作者提供）",
    "opening_scene": "xxx上门找茬，主角刚穿越打算先看看情况，不行就跑",
    "target_chapters": 20,
    "status": "active",
    "chapters_written": 0,
    "running_outline": "本弧到目前为止的连贯故事线（每写一章后更新）",
    "next_direction": null,
    "suggestions": []
  }
}
```

`status` 取值：`active`（可继续写）｜`awaiting_direction`（等作者给下一章方向）｜`completed`（本弧写满，等开下一弧）。

## 弧大纲生成：哲学差异（A 的灵魂）

`generate_arc_outline` 的 prompt 必须**显式约束**：

- 只规划**当前这一段连贯故事**（≈`target_chapters` 章，默认 20）；
- **禁止**规划几百/几千章后的大伏笔、终局、跨弧悬念；
- 大纲是"软骨架"：给每章一个 beat 方向，但允许作者每章用 `next_direction` 改写走向；
- 钩子只做**弧内**钩子，不埋超出本弧的线。

这与旧 `outline_generator`（书→卷→章全量 750 章）是**相反哲学**，故新建独立服务而非改旧的。

## 逐章循环：状态迁移与终止

```
start_arc → status=active, chapters_written=0
写一章 → chapters_written += 1, running_outline 更新
  ├─ chapters_written < target_chapters → status=awaiting_direction（等作者方向）
  └─ chapters_written >= target_chapters → status=completed（弧满）
作者给 next_direction（status=awaiting_direction）→ 回到 active 可写下一章
弧 completed → build_arc_completion_suggestions 给 N 个下一弧建议 + 问开场
```

**纯状态逻辑**（`advance_arc_state`）可在无 LLM 下单元测试。

## API 端点（`/api/arc`）

| 方法 | 路径 | 作用 |
|------|------|------|
| POST | `/api/arc/start` | 用点子+背景+开场建第一弧：生成 kickoff 问题（可选）或直接生成弧大纲 + 建 Volume + Outline(_arc) |
| GET | `/api/arc/{project_id}/current` | 返回当前活跃弧的状态（status/chapters_written/target/running_outline/suggestions） |
| POST | `/api/arc/{project_id}/next-direction` | 作者提交下一章方向，写入 _arc.next_direction，status→active |
| POST | `/api/arc/{project_id}/chapter-brief` | 组装下一章 brief（弧大纲 beat + next_direction + 前章末尾），供生成端用；写章后更新弧状态 |
| POST | `/api/arc/{project_id}/complete` | 标记弧 completed，生成下一弧建议 |
| POST | `/api/arc/{project_id}/next-arc` | 用作者提供的下一弧场景建 volume_idx+1 的新弧 |

章节正文生成仍走既有 `/api/generate/chapter`（内部已是 `run_chapter_pipeline`）；A 只负责"弧编排 + 下一章 brief"，不重复实现生成。

## Echo / 不污染

弧状态查询只返回约定字段；LLM 生成弧大纲/建议的中间推理不落主流程，只存最终 `running_outline` / `suggestions`。

## 降级与边界

- kickoff 问题生成失败 → 跳过补全问答，用作者原始输入直接生成弧大纲（不阻断）。
- 弧大纲生成失败 → 返回错误事件，不建半截 Volume（事务回滚）。
- `target_chapters` 缺省 20，可由作者覆盖（4–40 夹紧）。
- 旧项目（无 `_arc` 标记的 volume）不受影响：`/api/arc/current` 对无 arc 项目返回 `null`，前端回退旧 wizard。
- `next-arc` 前必须当前弧 `completed`，否则 409。

## 测试策略（全 TDD，mock LLM，不打真 LLM）

1. **arc_loop 纯状态**：`advance_arc_state` 各迁移（active→awaiting→active→completed）、target 夹紧、target 达成判定。
2. **弧大纲生成**（mock LLM）：prompt 含"禁止千章伏笔/只规划本弧"约束；返回结构化 beats；失败降级。
3. **kickoff 问题**（mock LLM）：返回 N 个问题；失败→空列表（跳过）。
4. **弧末建议**（mock LLM）：返回 N 个下一弧建议。
5. **API 契约**（mock arc_loop）：start 建 Volume+Outline(_arc)；current 返回状态；next-direction 改 status；complete 生成建议；next-arc 要求 completed（409 守卫）；无 arc 项目 current=null。
6. **回归**：现有 666 pytest 全绿；新表无（零迁移）。

## 非目标（YAGNI）

- 不做前端向导大改（API 完备即可，前端可后续按既有 wizard 模式接；本环境无法有效测前端）。
- 不改 B 的 `run_chapter_pipeline` 内部。
- 不动旧 `outline_generator`（全量大纲）路径——它降级保留给需要的项目。
- 不引入新数据库表/迁移（弧状态寄存既有 Outline.content_json）。
- 不做跨弧大伏笔管理（与本子项目哲学相悖；既有 foreshadow 设施另说）。

## 交付口径

- `cd /root/ai-write/backend && .venv/bin/python -m pytest`，全绿。
- 前端 `tsc --noEmit` 不受影响（本轮不动前端）。
- 部署：`docker compose up -d --build backend`（已授权模式）。

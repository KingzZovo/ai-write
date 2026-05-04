# PR-OUTLINE-DEEPDIVE · 大纲生成管道重构设计文档

**时间**：2026-05-04
**作者**：B1 收尾会话
**分支**：`feat/phase2-fix`
**起点 HEAD**：`a147fc7`（Issue C 完成后）
**关联讨论**：本会话 2026-05-04 用户反馈「章节大纲只是把分卷大纲拄出来」

---

## 1. 问题陈述

用户准确提出三点严重不一致：

1. **章节大纲 = 分卷 chapter_summaries[i] 的直拷贝**。代码铁证：
   - `backend/app/api/volumes.py:313` 创建分卷时 `Chapter(…, outline_json=cs)`
   - `backend/app/api/outlines.py:142` PR-OL9 级联：`ch.outline_json = cs`
   - 全后端 grep `outline_json=` **无第三个写入点** —— 本质没有任何 LLM 扩写步骤。

2. **章节大纲 schema 只有 4 字段**：`{chapter_idx, title, summary, key_events}`。缺少「全本记忆」跳板字段：上章余波 / 本章状态变化 / 本章埋的伏笔 / 本章兼现的旧伏笔 / 下章钩子。

3. **分卷大纲 raw_text 可能结构破损**（用户贴的样本中 chapter_summaries 数组被携平）。抽查卷 3 原始 raw_text 结构正常，卷 1 可能被 LLM 生成时写坏 —— 需临清查。

## 2. 设计目标

重建大纲三层职责边界与扬镶关系：

```
全书大纲 (book outline)
   竞争背景、核心冲突、人物圣杯、主题费调、卷主线、起起处竞争背景
         ↓ 扩写产生
分卷大纲 (volume outline)
   本卷核心冲突、转折点、本卷伏笔表、出场/退场人物、本卷状态走向
         ↓ 扩写产生 (现在缺这一步)
章节大纲 (chapter outline)  ·  每章一独立 LLM 调用
   上章余波、本章主事件链、本章状态变化、本章伏笔（埋/兑）、下章钩子
         ↓ 扩写产生
章节正文
```

独立化 LLM 扩写是必须的，理由：

- 分卷大纲在生成时需要一次输出 N 章（3~30）余要，LLM 上下文窗口被限制；详细字段挥在那一批会质量崩。
- 独立扩写可以拿「全书 + 分卷 + 上章 outline + 上章正文 (如有) 」作为上下文，有源动力去填充「余波 / 状态变化」这种需要跳板智能的字段。
- 调一次输出小一个章节 outline，质量控制容易，不受输出长度拖累。

## 3. 新 schema 定义

### 3.1 章节大纲 (chapter outline) 深化 schema

```jsonc
{
  // 现有字段（不动）
  "chapter_idx": 1,
  "title": "雨夜外滩",
  "summary": "本章 30~50 字概描",
  "key_events": ["事件 1", "事件 2"],

  // PR-OUTLINE-DEEPDIVE 新增字段
  "prev_chapter_threads": [
    "从上章承接的未完调事 1 (冲突 / 状态 / 悬念)",
    "…"
  ],
  "state_changes": {
    "characters": [
      {"name": "沈砚凉", "change": "拾到怀表，间接授件 ‘沈衡旧案’ 该索"},
      "…"
    ],
    "items": [
      {"name": "黑色怀表", "change": "出现于沈砚凉扣子暑袋，指针停于 9:17"}
    ],
    "relationships": [
      {"from": "沈砚凉", "to": "顾远舟", "change": "从陬生转为警觉各犹"}
    ]
  },
  "foreshadows_planted": [
    {
      "description": "怀表指针停于 9:17",
      "resolve_conditions": "后续某章揭示该时间点与父亲失踪事件同斶"
    }
  ],
  "foreshadows_resolved": [
    "之前某章埋下的“表链断口”伏笔被本章检查表背面动作兑现"
  ],
  "next_chapter_hook": "沈砚凉返家后准备查表背面刻字，下章开场必须接这个动作"
}
```

### 3.2 分卷大纲 (volume outline) 加固 schema

现有 schema 已足够，主要负责 prompt 强制严格输出，同时补 1 个跳板字段：

```jsonc
{
  "volume_idx": 1,
  "title": "外滩怀表",
  "core_conflict": "…",
  "turning_points": ["…"],
  "new_characters": [{"name": "…", "role": "…", "identity": "…"}],
  "departing_characters": ["…"],
  "foreshadows": {
    "planted": [{"description": "…", "resolve_conditions": "…"}],
    "resolved": ["…"]
  },
  "chapter_count": 30,
  "chapter_summaries": [ /* 不变，仍然是 4 字段列表，深化交给章节扩写器 */ ],
  // 新增字段
  "transition_to_next_volume": "本卷末尾状态 → 下卷开篇状态的街接"
}
```

## 4. 实施计划 · 按依赖顺序拆 4 阶段

| Phase | 产出 | 依赖 | 估计 |
|---|---|---|---|
| **Phase 1** | `chapter_outline_expander.py` + celery 任务 + 手动 API 触发点（并不后台同步生成）。烟测以测试项目卷 1 章 1 跱一次，输出与 DB 贤质量。 | 无 | 4-6 PR |
| **Phase 2** | volume / chapter outline prompt 结构加固；backend writer 所有路径 strict 补默认；FE OutlineEditor 结构化表单表示·raw_text 备份 | Phase 1 schema 击齐 | 3-4 PR |
| **Phase 4** | `chapter_generator.py` 拉取章节 outline 新字段注入 prompt | Phase 1 章节 outline 能产生 | 1-2 PR |
| **Phase 3** | 历史 raw_text 破损扫描+修复脚本 | 不阻塞其他 phase | 1 PR |

## 5. Phase 1 详细设计

### 5.1 新文件 `backend/app/services/chapter_outline_expander.py`

输入：
- `project_id`
- `chapter_id`

上下文准备：
- `book_outline.content_json`
- 本章所属卷的 `volume_outline.content_json` 全体
- `chapter_summaries[chapter_idx-1]`（3~5 字段 stub）
- 上一章的 `Chapter.outline_json` 如已扩写，否则其 `chapter_summaries[i-1]`
- 上一章的 `content_text` 后段不超 1500 字 作为「近文」提醒 LLM。转 全书 / 分卷 / 上一章 outline JSON 加近文会拼拼进 prompt 。

Prompt 草案（中文）：

```
你是一名高质量中文小说大纲作者。依据以下上下文，为本章写一份可执行、含跳板资产、含状态变化的章节大纲。

【全书大纲】…
【本卷大纲】…
【上一章的大纲】…或「本章为卷首、无上章」
【上一章近文】…或「本章为全书首章」
【本章 stub（来自分卷大纲）】…

输出严格以以下 JSON 返回，不要有多余文本：
{
  "chapter_idx": 1,
  "title": "…",
  "summary": "30~50 字",
  "key_events": ["事件 1", …],
  "prev_chapter_threads": [“上章承接的未完冲突 / 未解决悬念”, …],
  "state_changes": {
    "characters": [{"name": "…", "change": "本章末尾人物状态变化"}],
    "items": [{"name": "…", "change": "道具/关键物件状态变化"}],
    "relationships": [{"from": "…", "to": "…", "change": "关系变化"}]
  },
  "foreshadows_planted": [{"description": "…", "resolve_conditions": "下次出现条件"}],
  "foreshadows_resolved": ["本章兑现的之前某伏笔描述"],
  "next_chapter_hook": "本章末尾留给下章的明确动作/冲突点"
}

要求：
- key_events 3~6 条，按时间顺序。
- prev_chapter_threads 必须与上章 next_chapter_hook 有可追溯联系（为首章可以为空数组）。
- foreshadows_planted 与 foreshadows_resolved 可为空，但列出时必须详细。
- next_chapter_hook 需与本章末尾场景强耦合。
```

LLM 调用：运用现有 `model_router.generate(task_type="outline_chapter_expand")`。

### 5.2 Celery 任务

`backend/app/tasks/outline_expand_tasks.py`（新文件）：

```python
@celery_app.task(name="outline_expand.expand_chapter")
def expand_chapter_outline_task(project_id: str, chapter_id: str):
    …
```

并发路线：为后续考虑，打包一个 `expand_volume_outlines_task(project_id, volume_id)` 在 volume 下所有 chapter 上击发。

### 5.3 API 触发点

`POST /api/projects/{project_id}/chapters/{chapter_id}/outline/expand`
- 返回 `{task_id, status}`
- 同时增一个 `POST /api/projects/{project_id}/volumes/{volume_id}/outlines/expand-all` 按卷起。

## 6. Phase 2 详细设计

- `outline_generator.py` volume prompt 里面 chapter_summaries 补上 `transition_to_next_volume` 位。
- chapters Sectioning 的 prompt 里加 explicit JSON Schema 强制。
- backend `_strip_volume_plan_tags` + `_parse_json` 存入前加一层 schema 检查，missing 字段 default empty list / dict。
- FE `OutlineEditor` 增一种 `viewMode: 'raw' | 'structured'` 选项，raw 伝 structured 均会在后端存为 `content_json.raw_text` + structured 字段。

## 7. Phase 4 详细设计

- `chapter_generator.py` 调用中拼加：
  ```
  【本章应连贯的上章余波】… prev_chapter_threads
  【本章要达成的状态变化】… state_changes
  【本章必须埋下的伏笔】… foreshadows_planted
  【本章可兑现的伏笔】… foreshadows_resolved
  【本章末尾要交接下章的动作】… next_chapter_hook
  ```
- 如果本章 outline_json 还是旧 4 字段格式，降级为现有逻辑同时 log warning。

## 8. Phase 3 详细设计

`scripts/migrate_volume_outline_raw_text.py`：

- 扫描所有 `outlines.level='volume'`
- 试解析 `content_json->>'raw_text'` 为 JSON
- 如果解析失败 / 缺少 chapter_summaries，打上 `tag: corrupted_raw_text=true`。
- 另外一个干跑 mode：重新渲染 `raw_text` 为 `json.dumps(content_json minus raw_text)`。

## 9. 风险与回退

- 新增 LLM 调用 · 成本上升：为测试项目 150 章 × · 中型模型 × 中等 prompt = ~5 元 RMB 手动赶起。用户需明确同意。
- LLM JSON 输出质量：需预设重试、schema 检查、fallback。
- 备份：每一批扩写前仅 dump `chapter.outline_json` 现状到旧表 column `outline_json_legacy`（或在 `versions` 表补一条 record）以便回滚。
- 如果后续发现 schema 无法完全友好 FE，可随时 disable expander 仅采用原拷贝逻辑。

## 10. 验收標准

烟测项目：`20d164ab-232f-4863-8265-452186638d83`（5卷×30章）。

- Phase 1 验收：手动触发卷 1 章 1 expand，`Chapter.outline_json` 后 7 字段全填充，质量人工看着不生硬。
- Phase 2 验收：从 UI 保存一次该章 outline 能看到 7 字段均能编辑。
- Phase 4 验收：重新生成同一章正文，所调 LLM prompt 中可看到 prev_chapter_threads / state_changes 被注入。完成章周边设定集中「物品恒含」静态调查能跳仓。
- Phase 3 验收：脚本扫出破损行数 = N，重渲染后为 0。

## 11. 下一步

本会话进行 Phase 1 完整实现。其余阶段顺序进行。

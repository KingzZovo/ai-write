# Iteration plan


## 2026-05-03 · 大纲流水线重构（v2 hierarchical outline pipeline）

背景：当前创建向导中，“生成全书大纲”跳过了分卷/章节大纲阶段，直接产生三卷×十章的硬规整骨架；`outline_generator.py` 的 `BOOK_OUTLINE_SYSTEM` 强制需要章节列表。

### 目标数据分层

| 层 | 存储 | 内容 |
|---|---|---|
| L0 全书 | `outlines.level="book"` | 书名/核心、主角小传、世界、三线占比、全书字数预算、卷规划列表 |
| L1 分卷 | `outlines.level="volume"` | 本卷剧情骨架、本卷章数（由字数推算）、每章承担的剧情节点 |
| L2 章节 | `chapter_outlines`（新表） | summary 300-500 字、appearing_characters、state_changes、strand_tag、foreshadow_ops、world_rule_refs、rag_plan、write_plan |
| L3 正文 | `chapters.content` | 正文 |

### PR 拆分

**PR-OL1：L0 prompt + 卷规划 schema**
- `outline_generator.py` `BOOK_OUTLINE_SYSTEM` 重写：去掉“九个部分”中的章节列表，改成必须输出 `卷规划` JSON：`[{卷名, 主题, 起承转合定位, 三线占比, 预计字数, 预计章节数范围}]`。
- L0 SSE done 事件带上 `volumes_planned` 数组；backend post-process 根据卷规划 N 创建空 Volume 行（不再写死 3 卷）。
- 验证：创建《城下听潮》后续项目，全书大纲中不出现“第N章”仅出现卷规划。

**PR-OL2：L1 分卷大纲闭环**
- `volumes` 表加 `plot_seed_json`（从 L0 拷入本卷任务书）。
- `outline_generator.generate_volume_outline()` prompt 重写：输入 = L0.卷规划[i] + 全局伏笔池 + Neo4j 关系快照；输出 = 本卷骨架 + 章节数（由模型估算） + 每章承担。
- 向导“第三步”代替现在的“三卷 x 十章”骨架生成，改为逐个卷调用 L1。
- 验证：每卷“暂无大纲”消失，点开有卷大纲内容。

**PR-OL3：L2 章节大纲结构化**
- 新表 `chapter_outlines(chapter_id PK, summary, appearing_characters, state_changes, strand_tag, foreshadow_ops, world_rule_refs, rag_plan, write_plan, content_version)`。
- `outline_generator.generate_chapter_outline()` 输入 = L1.本章承担 + 上一章末状态 + 章前 RAG 包；输出为上述 7 字段的 JSON。
- 验证：选中任一章节可看到 300-500 字摘要 + 本章出场人物 + 伏笔操作清单。

**PR-OL4：L3 正文生成补强 + RAG plan 执行**
- `generate_chapter` 读 L2.summary + L2.rag_plan 并按计划拉取上下文。
- 章末执行 L2.write_plan（PG: character_states/foreshadows/relationships UPDATE；Neo4j: relationship MERGE）。
- 验证：生成一章后，下一章 RAG bundle 能拿到最新状态。

**PR-OL5：向导 UI + 右侧查看入口**
- 完善 GeneratePanel `onViewOutline` 发出后的查看序列：drawer 列 L0 / 点击卷看 L1 / 点击章节看 L2。

### 验收指标
- 创建一个项目后，全书大纲 = 卷规划（不含章）。
- 卷个数 跨 1–10，不是死 3。
- 每卷 各自有 L1 outline。
- 每章 有 L2 outline。
- 生成正文时，prompt 输入含 L2.summary + L1.本章承担 + 最新 character_states。


## 2026-05-03 (补) · 状态重复 + 同名通用角色修复

背景：闻栏枝 10 状态 vs 9 distinct；院监A/院监B/院监 多名同实体；出租车司机/实习护士等 18 个通用名 2 条但 1 distinct；chapter_start=0 出现 47 次。

### Hot fixes (本轮已完成)
- entity_timeline.update_character_state 加带 "与上一条相同则跳过" 防护
- chapter_idx 强制 ≥ 1
- 前端 CharacterCardPanel 显示时 dedupeStates() 相邻同状态合并
- 前端对通用名加 ⚠ “可能多实例” 警示。

### 后续 PR-OL6（抽取 prompt + 同名区分）
- entity_extractor prompt 加规则：
  1. 仅输出本章“有变化”的状态。未变化不要按顶。
  2. 通用职业/称谓作人物名时必须加场景修饰词：如 “第2章医院护士」 / “末路出租司机」。
  3. 同一人物多场景出现且作者未明确是同一人，不要合并为一个 Character 实体。
- entity_tasks PG bulk insert 同步加 “与上一条相同则 SKIP”
- 数据修补脉本：合并现有 status_json 相同的相邻记录。


## 2026-05-03 (PR-OL1) · 卷规划 从 prompt 驱动 → 结构化 JSON

背景：之前 “七、分卷规划” 仅要求 LLM 写 “至少 3 卷” 纯文本，创建向导需要 detectVolumeCount() 正则反推卷数，默认 fallback 3。

### Change
- BOOK_OUTLINE_SYSTEM / BOOK_OUTLINE_SKELETON_SYSTEM：去掉 “至少 3 卷” 硬编码，prompt 明确 “根据创意/节奉自由决定 2-8 卷”；要求在段末输出 <volume-plan> JSON 块 (idx/title/theme/core_conflict/est_chapters)。
- OutlineGenerator._extract_volume_plan()：负责容错解析 (容忍 ```json 栈、疑似 JSON 结构)。
- staged_stream done event：多一个 volume_plan 字段。
- 前端 DesktopWorkspace.tsx：接收 evt.volume_plan 后自动 prefill volumeCountInput；在 step 2 顶部显示“📜 AI 推荐卷规划”卡片，列出每卷 卷名、预估章数、主题、冲突。

### Verification
- backend syntax: `python -c "import app.services.outline_generator"` → ok
- frontend `tsc --noEmit` → 无新增错。
- E2E：新建项目，全书大纲 staged 生成 → 调试 控制台 SSE 中看到 done.volume_plan；跳到 step 2 后顶部出现卷规划卡片，“共 N 卷” 已自动填入。
- 未变：detectVolumeCount() 作为 fallback 仍保留（当 LLM 忘记输出卷规划块时）。

### Next
- PR-OL2：knowledge_tasks outline_book 后台任务读 volume_plan 创建空 Volume 行（不再需要 手动“生成分卷大纲” 打拾）
- PR-OL3：卷规划卡片可点击编辑 (调整卷名/章数后创建项目)


## 2026-05-03 (PR-OL2) · 后台任务读 volume_plan 创建空 Volume

### Change
- backend/tasks/knowledge_tasks.py auto-save outline：
  * 生成完 outline_book 后，调 OutlineGenerator()._extract_volume_plan(full_text)
  * 将 volume_plan 一并写入 Outline.content_json (除了 raw_text)
  * 根据 volume_plan 中的 idx/title/theme 创建空 Volume 行 (idempotent：existing_idx 跳过)
- backend/api/generate.py SSE auto-save：同样从 full_text 提取 volume_plan 写入 content_json

### Verification
- backend syntax ok
- E2E: 新建项目走 outline_book 任务 → outlines.content_json["volume_plan"] 存在
- E2E: volumes 表出现 N 个空行 (idx=1..N，title = 卷名)

### Next
- PR-OL3: 前端卷规划卡片可点击编辑
- PR-OL4: detectVolumeCount() fallback 依然保留；volume_plan 不在时验证补忙路径


## 2026-05-03 (PR-OL3) · 卷规划卡片可编辑

### Change
- backend/api/outlines.py：新增 PATCH /api/projects/{pid}/outlines/{oid}/volume-plan
  * 请求体 {volume_plan: [...]}
  * 更新 outline.content_json["volume_plan"] (flag_modified 触发 SA 检测)
  * 同步更新现有 Volume.title (匹配 idx)
- frontend/DesktopWorkspace.tsx：
  * 卡片右上角 “✎ 编辑” 按钮
  * 点击后每行变成 input(卷名) + number(章数)
  * 保存 按钮 调 PATCH 接口；取消还原
  * editingPlan / savingPlan state

### Verification
- backend syntax ok
- E2E：生成全书大纲 → step2 点“编辑”→ 改卷名、章数 → 保存 → 查 outlines 表 content_json["volume_plan"] 已更新；volumes 表 title 已更新。

### Next
- PR-OL4: detectVolumeCount fallback 路径验证 (无 volume_plan 时 仍可创项目)
- PR-OL5: outline edit 编辑后 试 cascade 重生 章节摘要 同步


## 2026-05-03 (PR-OL4) · fallback 提示卡片

### Change
- frontend/DesktopWorkspace.tsx step 2:
  * 当 volumePlan 为 null 且 outlinePreview 存在时，顶部显示琰珀色 fallback 卡片
  * 提示“AI 未输出结构化卷规划” + detectVolumeCount() 探测的卷数
  * 引导用户手动调整下方 input 或返回修改大纲

### Verification
- frontend tsc + build OK
- E2E：如果 LLM 输出中缺 <volume-plan> 块，step 2 顶部出现琰珀色提示卡片

### Next
- PR-OL5: outline edit 后 cascade 重生章节摘要


## 2026-05-03 (PR-OL5) · 卷规划保存后提示 (轻量 cascade)

### Change
- frontend/DesktopWorkspace.tsx:
  * 新增 planSaveNotice state
  * 卷规划 PATCH 保存成功后，检查 volumes.length：
    - 如存在 N 个分卷大纲：提示 “检测到已生成 N 个分卷大纲，如果卷名/章数有变动请手动删除重生”
    - 如为空：提示 “点生成分卷大纲开始创建”
    - 失败时提示重试
  * Toast 位于卷规划卡片与 fallback 卡之间，可手动关闭

### Note (未做)
- 未自动删除老 Volume。完整 cascade 重生需要判断 “哪些 volume 名字/章数 变了就 invalidate”，
  但实际上“卷名稍变”不应该作为 invalidate 信号。仅 “章数变动大” 才应重生。
  这部分需要产品判断，先交给用户手动控制。

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

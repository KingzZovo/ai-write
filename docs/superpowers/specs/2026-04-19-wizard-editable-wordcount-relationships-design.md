# 向导可跳转可编辑 + 字数目标 + 单卷重生 + 关系表 + 一批 bug 修复

Status: design draft, pending user review
Date: 2026-04-19

## 目标

用户在"测试"项目报出的 11 个问题一次处理。分两大块：

1. **硬 bug（#1-7）**：设定集崩溃、关系图无边、偏好刷新丢失、空壳卷、卷数识别漏掉前传、加载慢、刷新后混乱按钮
2. **设计变更（#8-11）**：向导步骤可跳可编辑、字数目标配置、单卷重生、关系表数据化

## A. 向导可跳转可编辑

**目标**：用户能在 Step 1/2/3 之间来回跳；每步当前内容可编辑保存。

**交互**：步骤指示器每个数字变成 `<button>`，点击 `setWizardStep(n)`。跳转不清 state。

**Step 1**：
- 总是显示 `outlinePreview`
- 顶部按钮「编辑」切到 textarea；保存 → `PUT /api/projects/{pid}/outlines/{oid}` 传 `{content_json: {raw_text: newText}}`
- 加载时 `loadProjectData` 把已存在的 book outline 写入 preview，Step 1 不再是空白

**Step 2**：
- 顶部仍是卷数 + 生成按钮：
  - 卷数为空 → 识别；全书已有 N 卷 outline 时按钮改叫「补齐缺失卷」，`for i in 1..count if not exists: generate(i)`
  - 非空 → 整体重生前先确认"会删除现有分卷"（不在此任务，用 Section D 的单卷重生即可）
- 下方每个已生成卷可折叠，面板显示 `VolumeOutlineBlock`；每个折叠框有「编辑」按钮，点击切 textarea；保存 → `PUT /api/projects/{pid}/outlines/{vol_outline_id}`

**Step 3**：
- 只读汇总：X 卷、Y 章、总字数目标、每章字数目标
- 主操作「开始创作」进入 editor 视图

**loadProjectData 改动**：
- `activeView` 默认改为 `wizard`（之前依赖 volumes/outlines 判断）
- 如果 volumes+chapters 都齐全，`wizardStep` 跳到 3，但不强制进 editor
- 不再在 editor 视图的"无选中章节"分支显示 "继续生成分卷" 按钮（改成"查看大纲"把 wizardStep 设 1 / 2）

## B. 字数目标

**数据**：
- `projects.settings_json`（现有 JSON 字段）存 `target_total_words: int | null` 和 `target_chapter_words: int | null`
- `chapters.target_words INTEGER NULL`（新列，Alembic 迁移）

**UI**：
- 项目列表卡片三点菜单加「项目设置」→ 打开 `ProjectSettingsModal`：字段 "全书目标字数"、"单章目标字数（默认）"，可留空。保存 → `PUT /api/projects/{id}` 合并 settings_json
- 章节编辑器标题右侧加 "目标字数 [N | 默认]" 行内输入。空则 revert 到 project 默认并显示灰文案 `(默认 <X>)`。修改 → `PUT /api/projects/{pid}/chapters/{cid}` 传 `target_words`

**Prompt 注入**：
- `outline_generator.generate_chapter_outline` 读 project.target_chapter_words，prompt 追加 "本章目标字数：约 N 字"
- `chapter_generator.generate_stream`（已存在）接受 `max_tokens` — 映射 target_words 到 max_tokens（约 1 字 1.5 token 估算），或者在 prompt 里加字数约束

**迁移**：
```python
op.add_column("chapters", sa.Column("target_words", sa.Integer(), nullable=True))
```

## C. 单卷重新生成

**后端**：`POST /api/projects/{pid}/volumes/{vid}/regenerate`，Streaming SSE。

流程：
1. 校验 volume 属 project
2. 事务：删除该 volume 的所有 chapters；删除 `level=volume AND parent_id=book_outline_id AND content_json.volume_idx=<vid>` 的 outline 记录
3. 读 book_outline、volume.volume_idx
4. 调 `OutlineGenerator.generate_volume_outline` 流式返回
5. 结束后：parseVolumeOutline → 更新 volume.title + volume.summary → 写新 outline 记录（content_json 结构化）→ 创建 chapters（按 chapter_summaries）
6. 末尾 yield `{status: 'done', chapters_created: N}`

**前端**：
- 侧栏卷节点三点菜单新增一项「重新生成大纲」
- 点击打开 `RegenerateVolumeModal`：提示"此操作将删除本卷 N 章内容和大纲，然后用 AI 重新生成"；有"取消"和"确认重生"
- 确认后 apiSSE 订阅流；在侧栏卷条目上显示"正在重新生成..."loader；done 后 `onChanged`

**单卷重生 vs 全体再跑**：本项目不支持在前端一键"清空所有分卷重来"；用户若要整体重建，只能一卷一卷走单卷重生。理由：一键重建会级联删除大量已有章节内容，风险高。

## D. 角色关系表

**DB**：新表 `relationships`
```python
class Relationship(Base):
    __tablename__ = "relationships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    source_id = Column(UUID(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False)
    target_id = Column(UUID(as_uuid=True), ForeignKey("characters.id", ondelete="CASCADE"), nullable=False)
    rel_type = Column(String(50), nullable=False)  # ally/enemy/mentor/lover/rival/family/other
    label = Column(String(200), default="")         # 关系描述短语
    note = Column(Text, default="")                  # 长描述
    sentiment = Column(String(20), default="neutral")  # positive/negative/neutral
    created_at = Column(DateTime(timezone=True), default=_utcnow)
```
索引：`(project_id)`, `(source_id)`, `(target_id)`

Alembic 迁移一张表 + 索引。

**API**（加到 `backend/app/api/settings.py`，复用现有 prefix）：
- `GET /api/projects/{pid}/relationships` → `{relationships: [...], total}`
- `POST /api/projects/{pid}/relationships`（单条）
- `POST /api/projects/{pid}/relationships/bulk`（数组，去重）
- `PUT /api/projects/{pid}/relationships/{rid}`
- `DELETE /api/projects/{pid}/relationships/{rid}`

**Extractor 增强**：`settings_extractor` system prompt 新增 `relationships` 顶级字段输出，结构 `{source_name, target_name, rel_type, label, note, sentiment}`。落库时在 `/outlines/{oid}/extract-settings` endpoint 里两阶段：
1. 先把 characters + world_rules 写入
2. 再把 relationships 映射（source_name/target_name → character_id，用 `{project_id, name}` 反查新创建的 character id），写 relationships 表

**前端**：
- `RelationshipGraph.tsx` 拉 `/relationships`；拆包 `{relationships, total}`
- 渲染 SVG 线带 label；按 sentiment 着色（positive=绿 / negative=红 / neutral=灰虚线）
- hover 高亮

**设定集编辑**：非此任务范围。用户可手动通过未来的"关系管理"UI（暂不做）或直接 API 管理。

## E. Bug 修复（7 条）

### E1. SettingsPanel 崩（`i.map is not a function`）

`frontend/src/components/panels/SettingsPanel.tsx:36,44`：
```typescript
// 错
const data = await apiFetch<Character[]>(...)
setCharacters(data)
// 改
const data = await apiFetch<{ characters: Character[]; total: number }>(...)
setCharacters(data.characters)
```

同 worldRules → `{ world_rules, total }`。

### E2. RelationshipGraph 同错 + 无关系数据

- Characters 拉取同 E1 拆包
- 删除那个不存在的 `/relationships` 降级逻辑；统一走 D 节加好的新端点 `/relationships`

### E3. WritingGuidePanel 刷新丢选择

`frontend/src/components/panels/WritingGuidePanel.tsx`：useState 初值读 `localStorage.getItem('writing-guide-prefs')`；每次修改写回。key 全局（不按项目分）。

### E4. StyleSelector + StructureSelector 同问题

`frontend/src/components/panels/GeneratePanel.tsx`：
- `_selectedStyleId` / `_selectedStructureBookId` 模块级变量改成 `localStorage.getItem(\`style:${projectId}\`)`
- StyleSelector / StructureSelector 接收 `projectId` prop；读写按 project 区分
- `getSelectedStyleId()` / `getSelectedStructureBookId()` 改签名接受 projectId

DesktopWorkspace 调用处传当前 projectId。

### E5. 空壳分卷

`handleGenerateVolumeOutlines`：
- LLM 返回后 if `parsed.raw_text && !parsed.title && !parsed.chapter_summaries`（纯 raw_text 无结构）→ 视为失败
- 失败则重试一次（同样参数再调一次）
- 二次仍失败 → 不创建 Volume，`wizardProgress` 追加 "第 N 卷生成失败"，继续下一卷

### E6. 卷数识别漏前传

`detectVolumeCount`：
- 保留原数字模式
- 新增关键词模式：`前传`、`外传`、`终章`、`番外`、`序卷` 各出现一次 +1（去重同义词）
- 最终 count = 数字卷数 + 关键词数

### E7. 加载慢 + 刷新混乱按钮

**慢**：后端 `list_chapters` 加 query param `?lightweight=true`，lightweight 模式返回不含 `content_text` 的字段。前端 `loadProjectData` 调 lightweight 版本。单章读取仍走 `get_chapter` 返完整。

**混乱按钮**：DesktopWorkspace 的 editor 视图 "!currentChapter && outlinePreview" 那段：
- 原有"继续生成分卷"按钮移除
- 改为两个链接按钮："编辑大纲"（跳 wizardStep=1）、"查看分卷"（跳 wizardStep=2）

## F. 非目标

- 编辑大纲的 undo/redo
- 字数强制上限（仅作为 prompt 提示）
- 项目级别协作指南偏好（暂全局）
- `Relationship` 的图形拖拽编辑 UI（仅 API + 可视化，编辑通过 settings 页或后续做）
- 批量"整本重生"（级联风险大，不做）

## G. 实现拆分（按依赖顺序）

| # | 任务 | 文件 |
|---|---|---|
| 1 | Alembic 迁移：`relationships` 表 + `chapters.target_words` | backend/alembic/versions/ |
| 2 | Relationship model + list/create/bulk/put/delete endpoints | backend/app/models/project.py, backend/app/api/settings.py |
| 3 | Extractor 扩展：relationships 提取 + 写入 | backend/app/services/settings_extractor.py, backend/app/api/outlines.py |
| 4 | chapters list lightweight 参数 | backend/app/api/chapters.py |
| 5 | Single-volume regenerate endpoint (SSE) | backend/app/api/volumes.py |
| 6 | 前端 bug E1-E4 修复（SettingsPanel, RelationshipGraph, WritingGuidePanel, GeneratePanel） | frontend/src/components/panels/ |
| 7 | 前端 E5（volume outline 失败判定 + 重试）+ E6（detectVolumeCount 扩展） | DesktopWorkspace.tsx |
| 8 | 前端 E7（lightweight chapter list + 修改混乱按钮） | DesktopWorkspace.tsx |
| 9 | 前端 Wizard 指示器可点击 + Step 1/2 编辑模态 | DesktopWorkspace.tsx |
| 10 | 前端 ProjectSettingsModal（字数目标） + ProjectCard 三点菜单加项 | frontend/src/components/project/ |
| 11 | 前端 ChapterTargetWords 行内编辑 | DesktopWorkspace.tsx |
| 12 | 前端 RegenerateVolumeModal + OutlineTree 菜单加项 + SSE 处理 | frontend/src/components/outline/ |
| 13 | 前端 RelationshipGraph 使用新 API + sentiment 颜色 | frontend/src/components/panels/RelationshipGraph.tsx |
| 14 | 生成时注入字数目标到 prompt | backend/app/services/chapter_generator.py |
| 15 | 构建、部署、冒烟 | - |

## H. 风险与折中

- **迁移改 chapters 表**：字段 NULL，线上已有章节不受影响
- **Extractor 调用角色关系**：依赖 Character 先创建。若 LLM 同时输出 characters + relationships，extractor 两阶段走：先 flush characters 再查 id 再写 relationships；这可能在单次事务完成（flush 后 id 可见）
- **单卷重生**：并发两次点击会出问题。前端按钮禁用 + 后端事务 + 幂等（重生时 DELETE 再插入）
- **整体"一起做"的规模**：15 个实现步骤，~4-6 小时体量。会分 5-8 个 commit


## 2026-05-03 · Workspace panels v2 (NaN 修复 + 人物卡重做)

**背景**：用户反馈三线平衡显示 NaN 章、伏笔追踪全部 Planted + NaN%、设定集点击报 `Cannot read properties of undefined (reading 'identity')`、角色关系图谱太乱。

**根因**全是前后端字段契约不匹配：
- StrandPanel 读 `data.last_quest_chapter`，后端实际在 `data.tracker.last_quest_chapter`（嵌套多一层）。
- ForeshadowPanel 读 camelCase `narrativeProximity / plantedChapter`，后端是 snake_case `narrative_proximity / planted_chapter`。另后端抽取出的 type 是 `plot/character/worldbuilding/mystery`，前端只认 `major/minor/hint`。
- SettingsPanel `apiFetch<Character[]>` 实际返回 `{characters: [...], total}` 包装；字段名 `profileJson` vs `profile_json`。
- RelationshipGraph 原为圆排图谱，人物 ≥ 30 重叠严重。

**改动**
1. **三线平衡**：`StrandPanel.tsx` 改为读 `response.tracker.*`，加 `Number.isFinite` 兑底，并能同时处理 `history` 在顶层或 tracker 嵌套两种形式。
2. **伏笔追踪**：`ForeshadowPanel.tsx` 字段全部换 snake_case；增加 plot/character/worldbuilding/mystery 中文 Label；状态中文（已埋 / 酝酿中 / 该收了 / 已收）+ tooltip 说明；增加类型下拉过滤；Active/All 按钮加 title。
3. **设定集**：`SettingsPanel.tsx` 处理 `{characters: [...]}` 与裸数组两种返回形式；读 `profile_json` snake_case；`world-rules` 亦同。
4. **人物卡**（新建 `CharacterCardPanel.tsx`越位原 RelationshipGraph）：合并人物简介 / 小传 / 状态时间线 / 出入向关系为可折叠卡片；支持按身份 / 按姓氏 / 不分组 + 搜索。`DesktopWorkspace.tsx` 接线。
5. **后端新增** `GET /api/projects/{pid}/character-states`：返回项目下 `character_states` 表全部记录（可选 `character_id` 过滤）；`status_json` 同时兼容 DB 里混存的 dict / JSON-string 表现。

**验证（6 面板 + 2 接口） · 项目 `0eaeff87...`**
```
1.styles            HTTP 200
2.strand            HTTP 200  dom=fire warn=1
3.foreshadows       HTTP 200  total=349
4.characters        HTTP 200  total=48
5.world-rules       HTTP 200  total=50
6.relationships     HTTP 200  total=59
7.character-states  HTTP 200  total=119
8.tokens            HTTP 200  (in 28246 / 15.9M / 19.0M all-time)
```

**对账 SQL**
```sql
SELECT count(*) FROM character_states WHERE project_id='0eaeff87-2f91-452c-812c-b4bcf2924fe2';
-- 期望 119
SELECT count(*) FROM characters WHERE project_id='0eaeff87-2f91-452c-812c-b4bcf2924fe2';
-- 期望 48
```

**文件**
- M `frontend/src/components/panels/StrandPanel.tsx`
- M `frontend/src/components/panels/ForeshadowPanel.tsx`
- M `frontend/src/components/panels/SettingsPanel.tsx`
- ?? `frontend/src/components/panels/CharacterCardPanel.tsx`
- M `frontend/src/components/workspace/DesktopWorkspace.tsx`
- M `backend/app/api/settings.py`

**迁移 / 入口**
- 工作区右侧抽屉 “角色关系” (drawerPanel='relationship') 现在渲染 `<CharacterCardPanel />`， `<RelationshipGraph />` 不再被使用（文件保留，以防后续反棔）。如需重新启用图谱可手动在 DesktopWorkspace.tsx 改回。



## 2026-05-03 · 人物卡 / 右侧按钮 / i18n / anti-AI 补充

**问题发现**
- `characters.profile_json` 48/48 全是 `{}`；人物详情实际存在 `character_states.status_json` 中。人物卡“加载不出”的根原。
- 4 个右侧生成按钮点击即重生，无查看入口、无确认。
- StylePanel/CascadePanel 多处英文文案未中文化。

**本轮变更**
- `CharacterCardPanel.tsx` v2：
  - 启发式重要程度分组（主角/关键剧情角色/配角/路人） = relCnt*3 + stateCnt*1。
  - “隐藏路人” 开关默认开。
  - profile + 最新 status_json 合并展示（绕开 profile_json 空表问题）。
  - 状态变化、人物关系完整入卡。
- `GeneratePanel.tsx`：3 个大纲按钮拆“📖 查看” / “↺ 重生”双态；重生弹 ConfirmModal。
- StylePanel + CascadePanel 中文化。
- 《江南综合写法》 anti_ai_rules 由 3 条 → 8 条，补充朱雀检测报告重灾区的 5 条反 AI 规则。
- `docs/ITERATION_PLAN.md` 追加“大纲流水线重构” 5 个 PR 拆分。

**依然待办（C 块）**
- 按 ITERATION_PLAN.md 拆五个 PR 调整 outline pipeline。

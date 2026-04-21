# v0.9.0 — 设定集一等公民 + 关系图 + 版本 Diff

**目标：** 把 v0.4 以来一直停留在 JSON 里的角色 / 世界观 / 关系提升为可视化、可编辑、可追溯变更的一等公民；给作者一个真正的“小说库”。

## 角色详情页

- 路由：`/projects/{id}/characters/{cid}`
- 模块：
  - 头部：头像 / 姓名 / 身份 / 题材标签
  - 能力成长表（卷 × 等级 / 实力值 / 技能），支持逐卷编辑
  - 关键装备 & 道具时间线
  - 所属势力变更
  - 最近章节出场率（来自 `text_chunks` 统计）
  - 相关关系列表（跳到关系编辑侧拉）
- 修改触发：写 `character.profile_json` 后自动重建对应 ContextPack 切片（失效缓存）

## 关系图

### 技术选型

- `d3-force` 力导向布局（替代 v0.4 的圆形布局）
- `react-force-graph-2d` 作为适配层
- 节点大小 = 出场次数；边颜色 = 情感极性（friendly/neutral/hostile）；边粗细 = 重要度

### 交互

- 拖点：新建关系（源 → 目标 → 弹窗填 label + sentiment + since_volume）
- 右键边：删除 / 编辑 label / 标记 “本卷终结”
- 双击点：打开角色详情页
- 顶部筛选：按卷（选卷 → 只显示该卷存在的关系）
- 底部时间线：拖动卷滑块 → 关系图随卷演变

### 数据扩展

`relationships` 表新增：
- `since_volume_id` (FK nullable)
- `until_volume_id` (FK nullable, 空=仍存在)
- `evolution_json` JSONB：`[{volume_id, label, sentiment, note}]`

迁移 `a0900000_v09_relationships_evolution` 增两列 + 一列 JSONB。

## 设定集页面升级

- `/settings-book`（现有）强化为：
  - 角色：卡片网格 + 筛选（题材 / 派系 / 存活状态）
  - 世界观：按 `category` 折叠分组
  - 关系图入口按钮
  - 全局搜索（名字 + 描述 + profile_json 内字段）

## 章节版本 Diff UI

- `chapter_versions` 表已在
- 新页 `/projects/{id}/chapters/{cid}/versions`
  - 左右两栏 diff（monaco-editor 的 diff 模式）
  - 顶部选择要对比的两个版本
  - 一键 “回滚到此版本”
  - 标签：`draft` / `critic_pass_1` / `critic_pass_2` / `final` / `manual_edit`

## 设定集变更日志

- 新表 `settings_change_log`：`id, project_id, actor_type(user|agent|critic), target_type(character|world_rule|relationship), target_id, before_json, after_json, reason, created_at`
- 所有 `PATCH /api/characters/{id}` 等端点强制写日志
- 新页 `/projects/{id}/changelog` 时间轴查看

## Context Pack 失效机制

- `characters.profile_json` 或 `relationships.evolution_json` 更新 → 写 Redis 键 `ctxpack:invalid:{project_id}`
- `ContextPackBuilder.build()` 启动时检查，若 invalid 则强制重算（忽略缓存）

## 验收标准

- [ ] 关系图拖线建关系，下一次生成 prompt 能读到
- [ ] 按卷滑动时间线，关系图变化符合 evolution_json
- [ ] 改一个角色的 location，下一章 ContextPack 反映新值
- [ ] 章节版本页可对比并回滚，回滚后 current_version 指针正确
- [ ] 所有设定变更在 changelog 页可追溯

## 工作量

约 8-10d（关系图 3d / 角色页 2d / Diff UI 1d / changelog 1d / 缓存失效 1d / 测试 + 打磨 1-2d）

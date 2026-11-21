# 项目管理页 + 删除/重命名/软删除/批量 + outline 显示修复

Status: design draft, pending user review
Date: 2026-04-19

## 目标

1. **Bug 修复**：工作区"全书大纲"位置显示错误（显示成了分卷大纲）。
2. **项目管理页**：把当前左侧下拉式项目选择器升级为 `/` 路由下的独立列表页（网格卡片）。
3. **增删改**：
   - 删除项目 / 卷 / 章节
   - 重命名项目 / 卷 / 章节
   - 批量删除（项目列表）
   - 软删除（仅项目级，配合 `/trash` 回收站）

## Bug 根因与修复

**根因**：老版本 `handleConfirmOutline` 会重复 POST 一条 `level='book'` 记录，内容是 `JSON.parse(outlinePreview)`。当用户误在分卷阶段点了"确认"时，产生了 `level='book' AND content_json.volume_idx` 存在的"假书籍大纲"，多达 5 条被标成 `is_confirmed=1`。`loadProjectData` 优先取 `is_confirmed=1` → 命中假记录 → 全书大纲位置显示分卷内容。

**修复**：loadProjectData 过滤掉 `level='book'` 且 content_json 含 `volume_idx` 键的记录。剩余 book outline 按优先级取：

1. `is_confirmed=1` 中 created_at 最早
2. `is_confirmed=0` 中 created_at 最早
3. 没有就留空

纯前端改动，不清理 DB 记录。配合"删除/清理大纲"能力（见第 6 节）让用户自己清老数据。

## 软删除数据模型

**范围决定**：仅 `projects` 表加 `deleted_at TIMESTAMP WITH TIME ZONE NULL`。volumes / chapters 不加——删卷/删章是硬删除，立即消失。

**理由**：回收站的主要价值是保护"一本书"这种重负载工件。卷和章节重生成成本低，且若每级都加软删除，级联、恢复、回收站列表都会复杂化（恢复卷时父项目可能已被清空？章节恢复是否触发父卷自动恢复？）。先做项目级，覆盖 90% 风险场景。

**列表查询**：`GET /api/projects` 新增默认 `deleted_at IS NULL` 过滤；`?trashed=true` 查回收站内容。`GET /api/projects/{id}` 若被软删则 404（避免用户经 URL 访问删除的项目）。

**删除行为**：
- `DELETE /api/projects/{id}`：默认软删（设 `deleted_at=now`）
- `DELETE /api/projects/{id}?purge=true`：硬删（从 trash 内彻底清理；走当前级联 FK）

**恢复**：`POST /api/projects/{id}/restore` 将 `deleted_at` 设回 NULL。404 条件仍允许命中 trashed 行（否则恢复不了）。

**Alembic migration**：单步迁移 `add_deleted_at_to_projects`，加可空列 + 给 `created_at` 以外无索引（未来 trash 少量查询不需要）。

## 路由与导航

| 路由 | 内容 | 未登录行为 |
|---|---|---|
| `/` | 项目列表页 | 重定向 `/login` |
| `/workspace?id=UUID` | 当前 DesktopWorkspace / MobileWorkspace | 重定向 `/login` |
| `/workspace`（缺 id） | 重定向 `/` | 重定向 `/login` |
| `/trash` | 回收站（软删项目列表 + 恢复 + 彻底清） | 重定向 `/login` |

### URL → Project 同步

Workspace 加载时：从 `useSearchParams().get('id')` 读 UUID。与 `projectStore.currentProject.id` 对比；不一致则 `fetchProject(id) → setCurrentProject → loadProjectData`。若 `id` 无效/对应项目被软删 → `router.replace('/')` 并吐 toast。

移除工作区侧栏下拉 + `selectorOpen` 等相关本地状态。

## 项目列表页（/）

### 布局

```
+--------------------------------------------------+
| 我的项目                     [回收站] [+ 新建项目] |
+--------------------------------------------------+
| [ ] 多选  [删除选中(0)]  当 batch mode 开启时显示 |
+--------------------------------------------------+
| ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ |
| │ 卡片1   │ │ 卡片2   │ │ 卡片3   │ │ 卡片4   │ |
| └─────────┘ └─────────┘ └─────────┘ └─────────┘ |
+--------------------------------------------------+
```

断点：`grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4`

### 卡片内容

- **顶部**：书名（大字，truncate）、右上角 ⋯ 三点菜单
- **中部**：类型 tag、梗概 2 行 line-clamp
- **底部**：创建时间（相对，如"3 天前"）、卷数 · 章节数 · 总字数
- **整卡**：click body → `/workspace?id=X`；三点菜单区阻止冒泡

### 三点菜单

- 重命名（打开 RenameProjectModal）
- 删除（打开 DeleteProjectModal）

### 多选模式

顶部"多选"按钮 toggle。开启后：
- 每张卡片左上角加复选框
- 卡片 body 点击改为 toggle 选择（不跳转）
- 顶部操作栏显示"删除选中(N)"按钮 → BulkDeleteConfirmModal
- 批量删除走循环 DELETE（后端无批量端点，每条单发，乐观并发）

### 新建项目

沿用 DesktopWorkspace 里已有的模态；提到 `/` 页面复用即可。

### 空态

"还没有项目"提示 + 新建按钮居中。

## 工作区改造

### 侧栏顶部

```
+---------------------------+
| ← 返回项目列表             |
| 当前书名（只读 h3）        |
+---------------------------+
```

点"←"调 `router.push('/')`。

### 拆除内容

- `selectorOpen`、`handleSelectProject`、项目下拉 UI、dropdown click-outside 处理
- `projectsLoaded` 状态相关初始化——列表现在在 `/` 页拉

### URL 同步

`useEffect` 监听 `searchParams.id`：和当前 project.id 不同则请求 `/api/projects/{id}`，成功则 `setCurrentProject + loadProjectData`。

### 侧栏卷/章节三点菜单

每行右端 hover 显示 `⋯`；点击打开小浮层菜单，内容：
- 重命名
- 删除

定位：`position: absolute` 相对于行；外点击关闭；Escape 关闭。

## 重命名

- **项目**：独立 RenameProjectModal（简单文本框 + 保存），调 `PUT /api/projects/{id} { title }`
- **卷**：侧栏三点菜单项，inline 文本框（把 title span 换成 input），回车保存/Esc 取消，调 `PUT /api/projects/{p}/volumes/{v} { title }`
- **章节**：同卷，`PUT /api/projects/{p}/chapters/{c} { title }`

## 删除确认

### DeleteProjectModal（type-to-confirm）

```
⚠ 删除项目"《书名》"

该项目将被移入回收站，可随时从回收站恢复。要永久删除，请进入回收站操作。

为确认删除，请输入书名：书名
[____________]

[取消] [删除 (灰，未匹配禁用)]
```

输入匹配：`input.trim() === project.title.trim()`（不含引号、书名号）。Enter 不触发删除。

### DeleteVolumeModal

```
⚠ 删除卷"《卷名》"

该卷及其下 N 章内容将被彻底删除，不可恢复。

[取消] [删除（红）]
```

### DeleteChapterModal

```
⚠ 删除章节"《章节名》"

该章节的内容将被彻底删除，不可恢复。

[取消] [删除（红）]
```

### BulkDeleteConfirmModal

```
⚠ 删除 N 个项目？

这些项目将被移入回收站。

[取消] [删除]
```

## 回收站（/trash）

### 布局

表格：书名 · 类型 · 删除时间 · 操作（恢复 · 永久删除）

### 操作

- **恢复**：`POST /api/projects/{id}/restore` → 清 `deleted_at`
- **永久删除**：二次确认（type-to-confirm），`DELETE /api/projects/{id}?purge=true`

### 空态

"回收站为空" + 返回按钮。

## 实现拆分

后端和前端分开，前端 migration 前先走。

| 顺序 | 步骤 | 文件 |
|---|---|---|
| 1 | Alembic：add `deleted_at` to projects | `backend/alembic/versions/` |
| 2 | Project model 加 deleted_at | `backend/app/models/project.py` |
| 3 | `list_projects` 查询加 `Project.deleted_at.is_(None)` + 支持 `?trashed=true` | `backend/app/api/projects.py` |
| 4 | `get_project` 读到 deleted 时 404 | 同上 |
| 5 | `delete_project` 改软删；加 `?purge=true` 分支 | 同上 |
| 6 | 新增 `restore_project` endpoint | 同上 |
| 7 | loadProjectData bug fix | `frontend/.../DesktopWorkspace.tsx` |
| 8 | 新组件 `ProjectListPage` | `frontend/src/app/page.tsx` 改造 |
| 9 | 新组件 `ProjectCard`、`RenameProjectModal`、`DeleteProjectModal` 等 | `frontend/src/components/project/` |
| 10 | 工作区拆除下拉 + URL 同步 + 返回按钮 | DesktopWorkspace / MobileWorkspace |
| 11 | OutlineTree 三点菜单 + inline rename | `frontend/.../OutlineTree.tsx` |
| 12 | 删除模态系列 | `frontend/src/components/delete/` |
| 13 | 批量选择模式 | `ProjectListPage` 内部状态 |
| 14 | `/trash` 页面 | `frontend/src/app/trash/page.tsx` |
| 15 | 构建/回归验证 | `next build` + 容器重建 |

## 非目标（本次明确不做）

- 卷/章节的软删除与回收站条目
- 自动过期清理（N 天后硬删）
- 拖拽排序项目卡片
- 项目归档（和删除区分的第三状态）

## 风险

- Alembic 迁移需要重启 backend 容器执行 `alembic upgrade head`。操作顺序：合代码 → `docker compose exec backend alembic upgrade head` → 重启 backend。若失败，revision downgrade。
- 批量删除项目串行 DELETE，N 大时延迟高；N<50 可接受，100+ 需要加后端批量端点（留作 follow-up）。
- type-to-confirm 对中文输入法不友好；考虑改为匹配到书名即可（允许尾随空格）。
- Mobile workspace 也要同步改 URL 读取，否则移动端进入时行为不一致——计划 11 节一并改。

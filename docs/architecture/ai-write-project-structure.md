# AI Write Project Structure

更新时间：2026-04-17

这份文档描述的是 `ai-write` 项目本身的代码结构，不是用户在系统里创建的小说项目数据。后续任何功能改动、排障、重构，都应优先参考这里的边界划分。

## 顶层目录

```text
ai-write/
├── backend/           FastAPI 后端、数据库模型、业务服务、测试
├── frontend/          Next.js 前端工作区、设置页、知识库、写法页
├── nginx/             反向代理配置
├── postgres-init/     PostgreSQL 初始化脚本
├── evaluate/          评估与报告产物
├── docs/              架构文档、设计文档、后续实施计划
├── docker-compose.yml 本地完整运行环境
├── README.md          项目说明
└── ITERATION_PLAN.md  迭代记录
```

## 后端结构

```text
backend/
├── app/
│   ├── api/                 HTTP 路由层
│   │   ├── projects.py      项目 CRUD / 导出
│   │   ├── outlines.py      大纲 CRUD / 确认
│   │   ├── volumes.py       卷 CRUD
│   │   ├── chapters.py      章 CRUD / 同步
│   │   ├── generate.py      大纲与正文生成接口（SSE + 异步任务）
│   │   ├── styles.py        写法档案 CRUD / 风格检测 / 试写
│   │   └── ...
│   ├── services/            业务逻辑层
│   │   ├── outline_generator.py  书/卷/章大纲生成
│   │   ├── style_detection.py    风格检测与规则提炼
│   │   ├── prompt_loader.py      PromptRegistry 兜底加载
│   │   ├── prompt_registry.py    内置 prompt 注册中心
│   │   └── ...
│   ├── models/              SQLAlchemy ORM 模型
│   ├── schemas/             Pydantic schema
│   ├── db/                  数据库与中间件连接
│   └── main.py              FastAPI 入口、middleware、router 注册
├── tests/                   API / service 测试
├── alembic/                 数据库迁移
└── pyproject.toml           Python 依赖定义
```

### 当前与写作主流程强相关的后端模型

- `Project`：书级项目主表
- `Outline`：书 / 卷 / 章三级大纲，`parent_id` 构成层级
- `Volume`：卷记录
- `Chapter`：章节记录，正文、状态、章级 outline 都挂在这里
- `StyleProfile`：写法档案，当前承载字段主要是 `rules_json`、`anti_ai_rules`、`tone_keywords`、`sample_passages`、`config_json`

### 当前与本次改造最相关的后端文件

- `backend/app/api/generate.py`
- `backend/app/api/projects.py`
- `backend/app/api/outlines.py`
- `backend/app/api/volumes.py`
- `backend/app/api/chapters.py`
- `backend/app/api/styles.py`
- `backend/app/services/outline_generator.py`
- `backend/app/services/style_detection.py`
- `backend/app/services/prompt_loader.py`
- `backend/app/services/prompt_registry.py`

## 前端结构

```text
frontend/
├── src/
│   ├── app/
│   │   ├── workspace/page.tsx   PC / 手机工作区入口
│   │   ├── styles/page.tsx      写法管理页
│   │   ├── knowledge/page.tsx   参考书 / 风格提取 / 架构提取
│   │   ├── settings/page.tsx    模型与系统设置
│   │   └── ...
│   ├── components/
│   │   ├── workspace/
│   │   │   ├── DesktopWorkspace.tsx
│   │   │   ├── MobileWorkspace.tsx
│   │   │   └── WorkspaceLayout.tsx
│   │   ├── outline/OutlineTree.tsx
│   │   ├── panels/GeneratePanel.tsx
│   │   └── ...
│   ├── stores/
│   │   ├── projectStore.ts
│   │   ├── generationStore.ts
│   │   └── knowledgeStore.ts
│   └── lib/
│       ├── api.ts
│       └── syncManager.ts
├── public/
├── package.json
└── next.config.ts
```

### 当前与本次改造最相关的前端文件

- `frontend/src/components/workspace/DesktopWorkspace.tsx`
- `frontend/src/components/workspace/MobileWorkspace.tsx`
- `frontend/src/components/outline/OutlineTree.tsx`
- `frontend/src/stores/projectStore.ts`
- `frontend/src/lib/api.ts`
- `frontend/src/app/styles/page.tsx`

## 运行时数据流

### 工作区主链路

1. 前端通过 `frontend/src/lib/api.ts` 调后端 `/api/...`
2. `DesktopWorkspace` / `MobileWorkspace` 负责项目选择、加载卷章、大纲向导、正文生成
3. 后端 `generate.py` 负责大纲和正文生成
4. 后端 `projects.py` / `outlines.py` / `volumes.py` / `chapters.py` 负责持久化
5. 前端 `projectStore.ts` 保存当前项目、卷、章、选中状态

### 风格学习链路

1. 用户在 `styles/page.tsx` 或 `knowledge/page.tsx` 发起风格检测
2. 后端 `styles.py` 调用 `style_detection.py`
3. `style_detection.py` 做两层工作：
   - 统计特征分析
   - LLM 深度风格分析
4. 分析结果被收敛成 `StyleProfile`
5. 生成章节时通过 `GeneratePanel` 选择 `style_id`，再传给生成接口

## 已确认的结构性问题

### 1. PC 工作区比手机工作区少了一整层“持久化视图”

PC 端的 `DesktopWorkspace.tsx` 在大纲向导里高度依赖本地状态：

- `outlinePreview`
- `wizardStep`
- `wizardProgress`
- `confirmedOutlineId`

刷新后这些状态全部丢失，只重新拉了部分持久化数据，所以会出现：

- 手机能看到部分大纲，PC 端看不到
- 自动创建了卷，但卷内没有可见的结构信息
- 用户无法回到“编辑整体大纲”的中间态

### 2. 书 / 卷 / 章大纲的持久化边界不清晰

当前同时存在两套行为：

- `generate.py` 在生成完成后自动往 `outlines` 表写一份 `raw_text`
- `DesktopWorkspace.tsx` 又会手动 POST 一次 outline

这会带来重复保存、父子关系不完整、前端无法可靠恢复的问题。

### 3. CRUD 能力后端已有，前端几乎没暴露

后端已经有：

- 删除项目
- 删除大纲
- 删除卷
- 删除章节

但工作区前端没有给出入口，也没有编辑整体大纲、删除卷章的交互。

## 文档使用原则

- 改动工作区主流程时，先看这份结构文档，再看对应设计文档
- 涉及新字段、新层级关系时，优先确认是落在 `Outline`、`Volume` 还是 `Chapter`
- 风格系统优先复用 `StyleProfile` 现有承载能力，必要时把更详细的分析报告先放进 `config_json`

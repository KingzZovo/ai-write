# AI Write - AI 驱动的全流程小说写作平台

全栈 AI 小说写作平台。AI 负责从大纲到正文的全部创作，人工仅做审阅和微调。支持学习已有小说的写作风格，分层记忆系统支撑 200-500 万字超长篇不出设定矛盾，条件触发式伏笔管理，6 维度独立质量审查，去 AI 味润色。

## 系统架构

```
                    Nginx (:8080)
                   /          \
        Next.js (:3100)    FastAPI (:8000)
        前端 UI              后端 API
        |                   |
        |              Celery Workers (异步任务)
        |                   |
        +------- 存储集群 -------+
        |           |          |        |
   PostgreSQL    Qdrant     Neo4j    Redis
   (业务数据)   (向量检索)  (知识图谱) (缓存/队列)
```

**技术栈：**
- **前端：** Next.js 16 + TypeScript + Tailwind CSS + Zustand + ProseMirror
- **后端：** Python 3.11+ / FastAPI + Celery + SQLAlchemy 2.0 (async)
- **存储：** PostgreSQL 16 + Qdrant + Neo4j 5 + Redis 7
- **LLM：** Anthropic / OpenAI / OpenAI Compatible / LoRA 微调模型（前端可配置多端点）

## 快速开始

### 1. 克隆并配置

```bash
git clone https://github.com/KingzZovo/ai-write.git
cd ai-write
cp .env.example .env
```

### 2. 启动服务

```bash
docker compose up -d
docker compose exec backend alembic upgrade head
```

8 个服务：PostgreSQL, Redis, Qdrant, Neo4j, FastAPI, Celery Worker, Next.js, Nginx

### 3. 访问

- **Web UI:** http://localhost:8080（登录：king / Wt991125）
- **API Docs:** http://localhost:8000/docs

### 4. 配置 LLM 端点

登录后访问 **设置** 页面，添加 LLM API 端点并分配到各任务：
- 大纲/章节生成 → 云端大模型（Claude / GPT）
- 摘要/提取 → 本地模型（Qwen / RWKV）
- 向量化 → 独立 Embedding 端点（Jina / BGE）

## 核心功能

### 全流程 AI 创作管道

```
创意输入 → AI 全书大纲 → AI 分卷大纲 → AI 章节大纲 → AI 章节正文
              ↓              ↓              ↓              ↓
           用户审阅         用户审阅       用户审阅       用户审阅
```

- **双 Agent 管道：** PlotAgent（剧情推演）→ StyleAgent（风格润色 + few-shot）
- **SSE 流式生成：** 实时打字机效果
- **批量生成：** 多章节队列式生成 + 进度追踪

### 三层上下文引擎 (Context Pack)

| 层 | 内容 | 占比 |
|----|------|------|
| L1 近距离层 | 前 5 章摘要 + 当前内容 + 本章大纲 + 未来 10 章方向 | 40% |
| L2 事实层 | 世界观 + SCORE 角色卡 + CFPG 伏笔三元组 + DOME 时间锚点 + 矛盾缓存 | 33% |
| L3 RAG 层 | CoKe 关键词检索 + 角色对话样本 + 风格 few-shot | 20% |

**高级技术：**
- **SCORE 动态追踪：** 角色位置/实力/关系/心理状态/近期行为实时追踪
- **CFPG 伏笔三元组：** 因 → 伏笔 → 消解目标，proximity 自动计算
- **DOME 时间锚点：** 关键事件 + 因果链追踪
- **CoKe 关键词检索：** 从大纲提取实体名触发向量检索
- **ToM 心智理论：** 角色心理状态建模
- **三线交织 (Strand Weave)：** Quest/Fire/Constellation 平衡监控

### 6 维度独立质量审查

| Checker | 检查内容 |
|---------|---------|
| ConsistencyChecker | 世界观规则违反、力量体系冲突 |
| ContinuityChecker | 时间线连续性、角色位置一致性 |
| OOCChecker | 角色 Out-of-Character 检测（5 种性格原型） |
| PacingChecker | 句长变化、张力曲线、信息密度波动 |
| ReaderPullChecker | 开头吸引力、微兑现分布、结尾钩子 |
| AntiAIChecker | 64 个 AI 高频词、四字成语密度、"的"字密度、句式单调性 |

6 个 Checker 并行执行，加权评分。

### 写作质量系统

- **7 大写作模块：** 展示非讲述 / 场景沉浸 / 对话技巧 / 张力控制 / 微观张力 / 情感共鸣 / 信息编织
- **13 种悬念钩子：** 突然揭示 / 紧急危机 / 身份反转 / 两难选择 / 留白钩子 等
- **12 个题材模板：** 玄幻 / 仙侠 / 都市 / 言情 / 悬疑 / 科幻 / 历史 / 末世 / 系统流 / 知乎短篇 等
- **12 条写作禁忌 + 64 个 AI 词库**

### 知识库与风格学习

- **Legado 书源引擎：** 兼容阅读 app 书源规则，支持排行榜浏览
- **文本清洗管道：** TXT/EPUB/HTML → 章节检测 → 噪音清洗 → 切片
- **风格提取：** jieba 分词 + 句长/对话比/修辞/视角统计分析
- **风格聚类：** DBSCAN/KMeans → 自动生成 StyleProfile
- **质量评分：** 5 维度 LLM 评估，低质量小说不参与学习

### LoRA 微调支持

```
云端服务器 (ai-write)  ←→  家用 GPU (RTX 5080 16GB)
  全栈 Web 平台              QLoRA 训练 + 推理
  云端 API (Claude/GPT)      Qwen2.5-7B / RWKV-7 7.2B
```

- 训练数据从已导入小说自动导出（Alpaca/ShareGPT 格式）
- 自动生成 Unsloth 训练脚本
- 推荐方案：14B 推理（质量）+ 7B 微调润色（风格）

## 项目结构

```
ai-write/
├── docker-compose.yml
├── backend/                         # 68 Python 文件, 16,654 行
│   ├── app/
│   │   ├── api/                     # 13 路由模块, 79 端点
│   │   │   ├── auth.py              # JWT 登录认证
│   │   │   ├── projects.py          # 项目 CRUD
│   │   │   ├── volumes.py           # 卷管理
│   │   │   ├── chapters.py          # 章节 CRUD + 同步
│   │   │   ├── outlines.py          # 大纲 CRUD + 确认
│   │   │   ├── generate.py          # SSE 流式生成
│   │   │   ├── knowledge.py         # 书源/导入/爬虫
│   │   │   ├── foreshadows.py       # 伏笔管理
│   │   │   ├── settings.py          # 角色 + 世界观
│   │   │   ├── versions.py          # 版本控制 + 评估
│   │   │   ├── quality.py           # 质量检查 + 指南
│   │   │   ├── model_config.py      # LLM 端点配置
│   │   │   ├── rewrite.py           # 文本改写 + 批量
│   │   │   └── lora.py              # LoRA 训练管理
│   │   ├── services/                # 22+ 业务服务
│   │   │   ├── context_pack.py      # 三层上下文引擎
│   │   │   ├── checkers/            # 6 独立质量审查器
│   │   │   ├── writing_guides.py    # 写作指南引擎
│   │   │   ├── strand_tracker.py    # 三线交织追踪
│   │   │   ├── model_router.py      # 多端点 LLM 路由
│   │   │   ├── memory.py            # 分层记忆金字塔
│   │   │   ├── entity_timeline.py   # Neo4j 实体时间线
│   │   │   ├── foreshadow_manager.py # 条件触发伏笔
│   │   │   ├── hook_manager.py      # Pre/Post 生成钩子
│   │   │   ├── book_source_engine.py # Legado 规则引擎
│   │   │   └── lora_manager.py      # LoRA 训练数据导出
│   │   └── models/                  # 17 ORM 模型
│   └── alembic/                     # 数据库迁移
├── frontend/                        # 31 TS/TSX 文件, 7,001 行
│   └── src/
│       ├── app/
│       │   ├── login/               # 登录页
│       │   ├── workspace/           # 工作区（核心）
│       │   ├── knowledge/           # 知识库管理
│       │   └── settings/            # 模型配置
│       ├── components/
│       │   └── panels/              # 13 个侧边栏面板
│       └── stores/                  # Zustand 状态管理
└── nginx/
```

## 项目管理与工作区

- **`/` 项目列表**：网格卡片，显示书名/类型/卷章统计；三点菜单支持**重命名 / 设置（字数目标） / 删除**；顶部"多选"进入批量删除
- **`/workspace?id=<uuid>`**：URL 驱动的工作区，侧栏顶部一键"← 返回项目列表"
- **`/trash` 回收站**：软删项目（30 天内可恢复）；输入书名二次确认可永久删除
- **生成向导**：步骤 1 / 2 / 3 可**任意跳转**；Step 1 全书大纲行内编辑；Step 2 每卷折叠面板单独编辑；Step 2 "补齐缺失卷"只生成缺口
- **字数目标**：项目级（全书 + 单章默认）+ 单章覆盖（章节标题旁行内编辑）；生成正文时注入 prompt "约 N 字 ±15%"
- **单卷重新生成**：侧栏卷三点菜单一键重生，SSE 流式进度，自动删旧章节与卷大纲再重建
- **角色关系表**：extractor 提取后落入 `relationships` 表；关系图显示带标签彩色线（正向绿、负向红、中立灰虚线）

## 前端面板

| 面板 | 功能 |
|------|------|
| 生成设置 | 模型/温度/字数配置（选择按项目持久化） |
| 质量评估 | 5 维度快速评分 |
| 质量检查详情 | 6 checker 详细报告 (SVG 环形指标) |
| 三线平衡 | Quest/Fire/Constellation 进度条 + 时间线 |
| 写作指南 | 7 模块开关 + 题材选择 + 禁忌清单（**偏好持久化到 localStorage**） |
| 去AI味检查 | 人味指数 + AI词检测 + 密度仪表 |
| 版本历史 | Git-like 分支/diff/切换 |
| 伏笔追踪 | planted→ripening→ready→resolved |
| 设定集 | 角色卡 + 世界观规则 |
| 角色关系 | SVG 关系图（带 label + sentiment 色） |
| 风格面板 | StyleProfile 选择 + 手动描述 |
| Token 用量 | 输入/输出/缓存命中率 |

## API 参考

**79 端点**，完整文档访问 `/docs`。

| 模块 | 前缀 | 端点数 |
|------|------|--------|
| 认证 | `/api/auth` | 2 |
| 项目 | `/api/projects` | 5 |
| 卷 | `/api/projects/{id}/volumes` | 4 |
| 章节 | `/api/projects/{id}/chapters` | 6 |
| 大纲 | `/api/projects/{id}/outlines` | 5 |
| 生成 | `/api/generate` | 3 |
| 知识库 | `/api/knowledge` | 12 |
| 伏笔 | `/api/projects/{id}/foreshadows` | 5 |
| 设定集 | `/api/projects/{id}` | 8 |
| 版本 | `/api/chapters/{id}/versions` | 5 |
| 质量 | `/api/chapters/{id}/check-*` | 5 |
| 模型配置 | `/api/model-config` | 8 |
| 改写 | `/api/rewrite` | 3 |
| LoRA | `/api/lora` | 4 |
| 统计 | `/api/stats` | 1 |

## 开发

```bash
# 后端
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -e .
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/aiwrite
alembic upgrade head
uvicorn app.main:app --reload --port 8000

# 前端
cd frontend && npm install && npm run dev

# Celery Worker
celery -A app.tasks:celery_app worker --loglevel=info

# Celery Beat (定时任务)
celery -A app.tasks:celery_app beat --loglevel=info
```

## 路线图

详见 [ITERATION_PLAN.md](ITERATION_PLAN.md)

## 设定集数据源约定（v1.9+）

为避免 Postgres 与 Neo4j 漂移，设定集相关实体（`world_rules` / `relationships` / `locations` / `character_states` 等）的约定是：

- **Neo4j 是真相源（source of truth）**：所有写入优先落 Neo4j
- **Postgres 是读优化投影（read model）**：通过 materialize 从 Neo4j 投影回 PG

常用写入口（**当前 main 实际可用**）：

- `POST /api/projects/{project_id}/outlines/{outline_id}/extract-settings`——Extractor 抽取 `characters / world_rules / relationships` 并写入 Neo4j，内部调用 `_materialize_entities_to_postgres()` 投影回 PG。入口：`backend/app/api/outlines.py:152`；materialize 函数：`backend/app/tasks/entity_tasks.py:47`。

计划中的入口（**v1.10 待实现**，本仓库任何分支都不包含）：

- `POST /api/projects/{project_id}/neo4j-settings/*`——通用 Neo4j 设定集写入接口（world-rules / relationships / locations / character-states）
- `POST /api/admin/entities/materialize`——手动刷新 PG 投影

> 文档漂移修正（2026-05-02）：以上两个路由族曾仅在未合并的中间提交中出现（commit `dc98363 feat(v1.9): add neo4j settings write API + materialize projection`、`08b0494 feat(v1.9): add admin materialize endpoint`），后续在 `feature/v1.0-big-bang` 重构中被删除；`origin/main` 与 `origin/feature/v1.0-big-bang` 双面均**不包含** `backend/app/api/neo4j_settings.py` / `backend/app/api/admin_entities.py`。详见 `docs/RUNBOOK.md §1` 与 `docs/HANDOFF_EXECUTION.md`。

**当前版本 v0.4.0** — 项目管理、工作区 UX、数据质量大幅提升：
- `/` 项目列表 + `/trash` 回收站 + 软删除/批量/重命名
- URL 驱动工作区 + 向导可跳可编辑
- 字数目标（项目/章节双级）+ 生成时注入 prompt
- 单卷一键重生（SSE 流式）
- `relationships` 表 + extractor 提取角色关系
- 7 个体验 bug 修复（设定集崩溃、偏好丢失、关系图无边、空壳卷、卷数漏识别、加载慢、混乱按钮）

**下一步：**
1. 测试套件 + CI
2. 写法引擎资产化（StyleProfile CRUD + Compiler + Runtime）
3. Prompt Registry 统一管理
4. 生产 Pipeline 状态机 + 断点恢复
5. 导出 EPUB/PDF
6. LoRA 训练 UI + RWKV-7 集成

## License

MIT

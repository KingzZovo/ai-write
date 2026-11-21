# AI-Write 全项目 Review 计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对 ai-write 项目进行全面 code review，覆盖代码质量、架构、安全性、性能、可维护性五个维度，输出可操作的改进建议清单。

**Architecture:** 项目是 FastAPI + Next.js 全栈，使用 PostgreSQL / Neo4j / Qdrant / Redis 四种存储，Celery 异步任务，通过 docker-compose 编排。后端 ~210 个 Python 文件，前端 ~80 个 TSX/TS 文件。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / Celery / Next.js 16 / React 19 / ProseMirror / Zustand / Docker

---

## 维度概览

| # | 维度 | 重点关注 |
|---|------|----------|
| 1 | 代码质量 | 命名一致性、文件大小、重复代码、类型安全、测试覆盖 |
| 2 | 架构 | 层次分离、服务依赖、数据流、模块边界 |
| 3 | 安全性 | 认证/授权、密钥管理、注入风险、CORS、依赖漏洞 |
| 4 | 性能 | N+1 查询、LLM 调用效率、缓存策略、前端包大小 |
| 5 | 可维护性 | 配置管理、文档、迁移策略、部署流程、错误处理 |

---

### Task 1: 代码质量审查

**Files:**
- Review: `backend/app/services/context_pack.py` (1930 行)
- Review: `backend/app/tasks/knowledge_tasks.py` (2370 行)
- Review: `backend/app/services/model_router.py` (1396 行)
- Review: `backend/app/services/prompt_registry.py` (956 行)
- Review: 所有 `frontend/src/components/panels/*.tsx`

**关注点:**
- 单文件超过 500 行的"上帝模块"拆分建议
- 前端 0 测试的风险评估
- 后端 88 个测试文件覆盖分布是否均匀
- 命名一致性（中英混用、snake_case vs camelCase）

- [ ] **Step 1: 识别超大文件并分析职责**

分析以下文件的职责边界：
- `context_pack.py` (1930行) — 是否可以拆为 layer builders + assembler
- `knowledge_tasks.py` (2370行) — 是否可以按 task_type 拆分
- `model_router.py` (1396行) — 是否可以拆为 router + provider adapters

对每个文件给出：当前职责数量、建议拆分方案、拆分后每个文件预估行数。

- [ ] **Step 2: 测试覆盖分析**

```bash
cd /root/ai-write/backend && python -m pytest --co -q 2>/dev/null | tail -5
```

Expected: 输出测试数量统计

统计每个 services/ 模块对应的测试数量，找出"零测试"服务模块。

- [ ] **Step 3: 前端代码质量扫描**

```bash
cd /root/ai-write/frontend && npx next lint 2>&1 | tail -20
```

检查 lint 规则覆盖度、TypeScript strict 模式是否开启、any 类型使用频率。

- [ ] **Step 4: 重复代码模式识别**

在后端搜索重复的错误处理模式、重复的 DB 查询模式、重复的 LLM 调用包装。
在前端搜索重复的 fetch 模式、重复的 UI 组件模式。

- [ ] **Step 5: 输出代码质量发现清单**

格式：`[严重程度] 文件:行号 — 问题描述 — 建议修复方式`

---

### Task 2: 架构审查

**Files:**
- Review: `backend/app/main.py` (371 行)
- Review: `backend/app/services/generation_runner.py`
- Review: `backend/app/graphs/generation_graph.py`
- Review: `docker-compose.yml`
- Review: `frontend/src/lib/api.ts`

**关注点:**
- 后端是否有明确的分层（API → Service → Repository）
- 循环依赖检测
- 前后端 API 契约是否有 schema 验证
- LangGraph 与自定义 state machine 是否重复

- [ ] **Step 1: 分层架构一致性检查**

验证是否所有 API 路由都通过 service 层操作数据库，有无跨层直接访问。

```bash
cd /root/ai-write/backend
grep -r "from app.models" app/api/ | grep -v "__pycache__" | head -20
grep -r "from app.db" app/api/ | grep -v "__pycache__" | head -20
```

Expected: 如果 API 层直接导入 models 和 db，说明分层不严格。

- [ ] **Step 2: 循环依赖检测**

```bash
cd /root/ai-write/backend
grep -r "from app.services" app/services/ | grep -v "__pycache__" | sort | head -30
```

分析 services 之间的互相依赖关系，画出依赖图，识别循环路径。

- [ ] **Step 3: 数据流分析 — 生成链路**

追踪一次章节生成的完整数据流：
`API generate → generation_runner → context_pack → model_router → SSE response`

识别：
- 哪些步骤是同步阻塞的
- 哪些应该是异步但不是
- 超时处理是否一致

- [ ] **Step 4: 前后端 API 契约**

检查前端 `api.ts` 中的端点定义与后端 router 的一致性。
是否有 OpenAPI schema 自动生成？前端是否消费了 schema？

- [ ] **Step 5: 输出架构发现清单**

---

### Task 3: 安全性审查

**Files:**
- Review: `backend/app/api/auth.py`
- Review: `backend/app/utils/crypto.py`
- Review: `backend/app/middlewares/quota.py`
- Review: `.env` / `.env.example`
- Review: `docker-compose.yml` (端口暴露)

**关注点:**
- JWT 实现是否安全（算法、过期、刷新）
- 密钥管理（.env 中已有真实 SECRET_KEY 和密码哈希）
- CORS 配置是否过于宽松
- SQL 注入风险（raw SQL 使用）
- 文件上传安全
- 依赖漏洞

- [ ] **Step 1: 认证系统审查**

```bash
cd /root/ai-write/backend
cat app/api/auth.py
cat app/utils/crypto.py
```

检查：
- JWT 算法是否使用 RS256/HS256
- token 有效期是否合理
- 是否有 refresh token 机制
- DISABLE_AUTH 环境变量在生产中的风险

- [ ] **Step 2: 密钥管理审查**

**严重问题已知：** `.env` 文件已提交到 git（含 SECRET_KEY 和密码哈希）。

检查：
- `.gitignore` 是否正确排除了 `.env`（已确认包含）
- `.env` 文件是否确实被 git 追踪（`git ls-files .env`）
- 生产密码是否足够强
- API key 加密存储的实现质量

- [ ] **Step 3: 注入风险扫描**

```bash
cd /root/ai-write/backend
grep -rn "text(" app/api/ app/services/ | grep -v "__pycache__" | grep -v "\.pyc" | head -20
grep -rn "f\".*{.*}.*\"" app/services/ | grep -i "select\|insert\|update\|delete" | head -10
```

检查是否有 raw SQL 拼接、LLM prompt injection 风险。

- [ ] **Step 4: 网络暴露面审查**

检查 docker-compose.yml 中哪些端口绑定到 0.0.0.0（当前都是 127.0.0.1，已是良好实践）。
检查 nginx 配置是否有路径穿越风险。

- [ ] **Step 5: 依赖安全扫描**

```bash
cd /root/ai-write/backend
pip audit 2>&1 | head -30
```

或者检查 requirements.lock 中是否有已知漏洞版本。

- [ ] **Step 6: 输出安全发现清单（按严重程度排序）**

---

### Task 4: 性能审查

**Files:**
- Review: `backend/app/services/context_pack.py` (DB 查询密度)
- Review: `backend/app/tasks/knowledge_tasks.py` (LLM 调用链)
- Review: `backend/app/services/ctxpack_cache.py`
- Review: `backend/app/services/semantic_cache.py`
- Review: `frontend/package.json` (bundle 依赖)

**关注点:**
- N+1 查询问题
- LLM 调用是否可并行化
- 缓存命中率和失效策略
- 前端首屏加载性能
- Celery worker 并发配置

- [ ] **Step 1: 数据库查询效率分析**

分析 `context_pack.py` 中的查询模式：
- 是否使用了 eager loading（joinedload / selectinload）
- 单次 build 调用触发多少次 DB roundtrip
- 是否有可以合并的查询

```bash
cd /root/ai-write/backend
grep -c "await.*db\.\|await.*session\." app/services/context_pack.py
grep -n "select(" app/services/context_pack.py | head -20
```

- [ ] **Step 2: LLM 调用链分析**

分析一次完整章节生成涉及多少次 LLM 调用：
- planning / drafting / critic / rewrite 各几次
- 是否有可以并行的调用
- 超时配置是否合理（当前看到 420s / 600s / 840s）

- [ ] **Step 3: 缓存策略审查**

检查 Redis 缓存的使用场景：
- context pack 缓存的 TTL 和 key 设计
- prompt cache 的命中率能否观测
- semantic cache 的实现是否有内存泄漏风险

- [ ] **Step 4: 前端性能分析**

```bash
cd /root/ai-write/frontend
cat next.config.ts
```

检查：
- 是否使用了代码分割
- ProseMirror + monaco-editor 的 bundle 大小影响
- 是否有不必要的客户端渲染

- [ ] **Step 5: 输出性能发现清单**

---

### Task 5: 可维护性审查

**Files:**
- Review: `PLAN.md` / `PROGRESS.md` / `ITERATION_PLAN.md`
- Review: `backend/alembic/` (迁移策略)
- Review: `scripts/` (运维脚本)
- Review: 各 `RELEASE_NOTES_*.md`

**关注点:**
- 配置散落（os.getenv 散布 vs 集中 Settings）
- 迁移文件是否可回滚
- 运维脚本是否有文档
- 错误处理策略一致性
- 日志结构化程度

- [ ] **Step 1: 配置管理一致性**

已知问题：`os.getenv` 散布在 20+ 处（knowledge_tasks.py, chapter_pipeline.py 等），而非集中在 `config.py` Settings 类中。

统计散落的环境变量数量，列出哪些应该收归 Settings。

- [ ] **Step 2: 错误处理模式审查**

分析 `context_pack.py` 中 38 个 try 块的模式：
- 是否有 bare except
- 是否吞掉了关键错误
- 错误日志是否包含足够上下文

- [ ] **Step 3: 数据库迁移健康检查**

```bash
cd /root/ai-write/backend
ls alembic/versions/ | wc -l
grep -l "def downgrade" alembic/versions/*.py | wc -l
```

检查：
- 是否所有迁移都有 downgrade
- 版本号命名是否有规律
- 是否有跳跃式 schema 变更（没有中间步骤）

- [ ] **Step 4: 项目文档与上手难度评估**

评估新开发者上手难度：
- README 是否包含完整的 setup 步骤
- docker-compose 一键启动是否可用
- 有无 API 文档（OpenAPI）
- 有无架构文档

- [ ] **Step 5: 运维脚本审查**

检查 `scripts/` 目录下的脚本：
- 是否有使用说明
- 是否有幂等性保证
- 是否处理了错误情况

- [ ] **Step 6: 输出可维护性发现清单**

---

## 总结输出格式

每个维度完成后，汇总为一份综合报告：

```markdown
## 综合评分

| 维度 | 评分 (1-10) | 关键风险 |
|------|-------------|----------|
| 代码质量 | ? | ? |
| 架构 | ? | ? |
| 安全性 | ? | ? |
| 性能 | ? | ? |
| 可维护性 | ? | ? |

## Top 10 必须修复项（按优先级）

1. ...
2. ...

## 建议改进路线图

### 短期（1-2 周）
- ...

### 中期（1-2 月）
- ...

### 长期（3+ 月）
- ...
```

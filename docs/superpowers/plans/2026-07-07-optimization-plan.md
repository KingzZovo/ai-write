# AI-Write 项目优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 基于全项目 Review 结果，系统性优化 ai-write 项目的安全性、性能、代码质量、架构、可维护性和前端体验（排除多用户和计费功能）。

**Architecture:** 按优先级分6个独立 Phase 执行，每个 Phase 产出可独立测试/部署的成果。Phase 之间有依赖关系但每个 Phase 内的 Task 可独立完成。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async / Celery / Next.js 16 / React 19 / Tailwind v4 / Vitest / Docker

---

## 执行状态（2026-07-26 收口）

| Task | 状态 | 说明 |
|------|------|------|
| 1 (.env.example 清理) | ✅ | commit 2d39c1c |
| 2/12 (CORS 环境变量化) | ✅ | commit 2d39c1c |
| 3/8 (os.getenv 收归 Settings) | ✅ | commit 2d39c1c |
| 4/7 (context_pack 查询合并) | ✅ | 已在 _build_proximity 单查询取章 + 合并 outline 查询（随 0af5efa 提交） |
| 5 (context_pack 文件拆分) | ⏭️ 跳过 | 判断：单一内聚域（dataclass+渲染+builder），拆散跨文件反伤导航；风险>收益 |
| 6 (knowledge_tasks 拆分) | ✅ | commit 1a02308：generation/book/analysis/common 四模块 + 兼容 facade |
| 9 (异常分级) | ✅ | context_pack 各层 SQLAlchemyError 上抛、其余降级带 project/ch 上下文日志（随 0af5efa） |
| 9' (前端测试框架) | ✅ | commit 612bdbf：Vitest + 12 tests |
| 10 (消除 any) | ✅ | projectStore 本已零 any；knowledge/styles/foreshadow/sentry 类型化，eslint 78→18 |
| 11 (ErrorBoundary) | ✅ | commit b37d11d |

剩余 10 个 eslint error 全部为 react-compiler `setState-in-effect`（行为级重构，另行排期）。

---

## Phase 概览

| Phase | 目标 | 预估耗时 | 依赖 |
|-------|------|----------|------|
| 1 | 安全修复 + 配置收归 | 2-3天 | 无 |
| 2 | 性能优化 (context_pack查询合并) | 2-3天 | 无 |
| 3 | 代码质量 (超大文件拆分) | 3-5天 | Phase 2 |
| 4 | 前端完善 (测试+类型安全+UI) | 3-5天 | 无 |
| 5 | 架构改善 (分层规范化) | 2-3天 | Phase 3 |
| 6 | 可维护性 (错误处理+文档) | 2-3天 | Phase 3 |

---

## Phase 1: 安全修复 + 配置收归

### Task 1: 清理 .env.example 中的真实密钥

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: 替换 SECRET_KEY 为占位符**

```bash
cd /root/ai-write
grep "SECRET_KEY" .env.example
```

将 `SECRET_KEY=yNvq6UT-Tk-tCosVlcWMoMEjQTvFkO1t34Mo2gUoLvCDu4J4IJYWwhKxeXigdumF` 替换为 `SECRET_KEY=change-me-generate-a-random-64-char-string`

- [ ] **Step 2: 清理密码哈希**

将 `AUTH_PASSWORD_HASH=$$2b$$12$$k2a...` 替换为 `AUTH_PASSWORD_HASH=` (空值，要求用户自行生成)

添加注释说明如何生成：
```
# Generate with: python -c "import bcrypt; print(bcrypt.hashpw(b'your-password', bcrypt.gensalt()).decode())"
```

- [ ] **Step 3: 验证 .env 不在 git 追踪中**

```bash
git ls-files .env
```

Expected: 空输出（未追踪）

- [ ] **Step 4: Commit**

```bash
git add .env.example
git commit -m "security: remove real secrets from .env.example, add generation instructions"
```

---

### Task 2: CORS 配置改为环境变量

**Files:**
- Modify: `backend/app/main.py:225-234`
- Modify: `backend/app/config.py`
- Modify: `.env.example`

- [ ] **Step 1: 添加 CORS_ORIGINS 到 Settings 类**

在 `backend/app/config.py` 的 Settings 类中添加：

```python
    # --- CORS ---
    CORS_ORIGINS: str = "http://localhost:3100,http://localhost:8080"
```

- [ ] **Step 2: 修改 main.py 使用 Settings**

将 main.py 中的硬编码 CORS 替换为：

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

- [ ] **Step 3: 更新 .env.example**

```
# CORS (comma-separated origins)
CORS_ORIGINS=http://localhost:3100,http://localhost:8080
```

- [ ] **Step 4: 运行测试验证**

```bash
cd /root/ai-write/backend && python -m pytest tests/test_api_auth.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/app/main.py .env.example
git commit -m "security: move CORS origins to env config, remove hardcoded values"
```

---

### Task 3: 收归散落的 os.getenv 到 Settings

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/tasks/knowledge_tasks.py` (12处)
- Modify: `backend/app/services/chapter_pipeline.py` (2处)
- Modify: `backend/app/middlewares/quota.py` (2处)
- Modify: `backend/app/api/generate.py` (4处)
- Modify: `backend/app/services/reference_ingestor.py` (6处)

- [ ] **Step 1: 在 config.py Settings 中集中定义所有散落变量**

```python
    # --- Generation ---
    SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS: float = 840.0
    SINGLE_SHOT_LLM_RETRY_ATTEMPTS: int = 1
    SINGLE_SHOT_LLM_STREAM: bool = False
    SINGLE_SHOT_LLM_BUDGET_ENDPOINTS: int = 2
    SINGLE_SHOT_FALLBACK_TIMEOUT_SECONDS: float = 420.0
    FORCE_DIRECT_CHAPTER: bool = False
    SCENE_MODE_TIMEOUT_HARD_CAP_SECONDS: float = 600.0
    CHAPTER_QUALITY_GATE_TIMEOUT_SECONDS: float = 420.0
    CHAPTER_PIPELINE_ENABLED: bool = True
    LOGIC_CRITIC_MAX_ROUNDS: int = 2
    CHAPTER_MAX_REWRITE_ROUNDS: int = 2
    QUALITY_GATE_PERSIST_ON_BLOCK: bool = True

    # --- Reference Ingestor ---
    REFERENCE_INGEST_CONCURRENCY: int = 20
    SEMANTIC_CHUNKER_MAX_TOKENS: int = 800

    # --- Quota ---
    DAILY_LLM_CALL_LIMIT: int = 0  # 0 = unlimited
    DAILY_TOKEN_LIMIT: int = 0
```

- [ ] **Step 2: 替换 knowledge_tasks.py 中的 12 处 os.getenv**

将所有 `_os.getenv("SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS", "840")` 替换为 `settings.SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS`。

在文件顶部添加：`from app.config import settings`

- [ ] **Step 3: 替换其他文件中的 os.getenv**

逐文件替换：
- `chapter_pipeline.py`: `os.getenv("CHAPTER_PIPELINE_ENABLED", "1")` → `settings.CHAPTER_PIPELINE_ENABLED`
- `chapter_pipeline.py`: `os.getenv("LOGIC_CRITIC_MAX_ROUNDS", "2")` → `settings.LOGIC_CRITIC_MAX_ROUNDS`
- `quota.py`: 替换限额相关变量
- `generate.py`: 替换超时相关变量
- `reference_ingestor.py`: 替换并发/分块相关变量

- [ ] **Step 4: 运行全量测试**

```bash
cd /root/ai-write/backend && python -m pytest --tb=short -q
```

Expected: 所有测试通过

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/app/tasks/knowledge_tasks.py backend/app/services/chapter_pipeline.py backend/app/middlewares/quota.py backend/app/api/generate.py backend/app/services/reference_ingestor.py
git commit -m "refactor: centralize 30+ scattered os.getenv calls into Settings class"
```

---

## Phase 2: 性能优化 — context_pack 查询合并

### Task 4: 为 context_pack 添加 eager loading 和查询合并

**Files:**
- Modify: `backend/app/services/context_pack.py:830-980` (_build_proximity)
- Modify: `backend/app/services/context_pack.py:983-1250` (_build_facts)
- Create: `backend/tests/services/test_context_pack_queries.py`

**目标:** 将单次 build() 的 DB roundtrip 从 21+ 次降到 7 次以下。

- [ ] **Step 1: 写测试 — 验证 build() DB 调用次数**

```python
# backend/tests/services/test_context_pack_queries.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.context_pack import ContextPackBuilder


@pytest.mark.asyncio
async def test_build_proximity_batches_queries(db_session):
    """_build_proximity should issue <= 4 DB queries total."""
    builder = ContextPackBuilder(db=db_session)
    query_count = 0
    original_execute = db_session.execute

    async def counting_execute(*args, **kwargs):
        nonlocal query_count
        query_count += 1
        return await original_execute(*args, **kwargs)

    db_session.execute = counting_execute
    # This test validates we don't regress on query count
    # after optimization. Exact count depends on data presence.
    # Target: <= 4 for proximity layer
```

- [ ] **Step 2: 合并 _build_proximity 中的 volume/chapter 查询**

当前模式（每次独立查询）：
```python
# 查询1: volume_idx
await db.execute(select(Volume.volume_idx).where(Volume.id == volume_id))
# 查询2: chapter summaries
await db.execute(select(Chapter.summary, Chapter.chapter_idx).where(...))
# 查询3: current chapter
await db.execute(select(Chapter).where(...))
# 查询4: upcoming outlines
await db.execute(select(Chapter.chapter_idx, Chapter.title, Chapter.outline_json).where(...))
# 查询5-7: outline content_json (3次)
```

优化为 2 次合并查询：
```python
from sqlalchemy.orm import selectinload

# 合并查询1: volume + 所有相关 chapters (一次 JOIN)
stmt = (
    select(Volume)
    .options(selectinload(Volume.chapters))
    .where(Volume.id == str(volume_id))
)
volume_result = await db.execute(stmt)
volume = volume_result.scalar_one_or_none()

# 从内存中筛选 chapters，不再逐条查询
if volume:
    chapters = sorted(volume.chapters, key=lambda c: c.chapter_idx)
    current_ch = next((c for c in chapters if c.chapter_idx == chapter_idx), None)
    prev_chapters = [c for c in chapters if c.chapter_idx < chapter_idx][-5:]
    upcoming = [c for c in chapters if c.chapter_idx > chapter_idx][:10]

# 合并查询2: outlines for this volume (一次批量)
stmt = select(Outline.content_json, Outline.level).where(
    Outline.project_id == str(project_id)
)
```

- [ ] **Step 3: 合并 _build_facts 中的 world_rules + characters + foreshadows**

当前：3 次独立查询。优化为 1 次并行执行：

```python
import asyncio

# 并行发起 3 个独立查询
world_rules_task = db.execute(
    select(WorldRule.category, WorldRule.rule_text)
    .where(WorldRule.project_id == pid)
)
characters_task = db.execute(
    select(Character).where(Character.project_id == pid)
)
foreshadows_task = db.execute(
    select(Foreshadow).where(Foreshadow.project_id == pid)
)

# 注意：SQLAlchemy async session 不支持真正并行
# 改用单次查询 + 内存分组
stmt = select(WorldRule).where(WorldRule.project_id == pid)
world_rules = (await db.execute(stmt)).scalars().all()

stmt = select(Character).where(Character.project_id == pid)
characters = (await db.execute(stmt)).scalars().all()

stmt = select(Foreshadow).where(Foreshadow.project_id == pid)
foreshadows = (await db.execute(stmt)).scalars().all()
```

- [ ] **Step 4: 运行测试验证**

```bash
cd /root/ai-write/backend && python -m pytest tests/services/test_context_pack_queries.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/context_pack.py backend/tests/services/test_context_pack_queries.py
git commit -m "perf: merge 21 DB roundtrips in context_pack.build() down to ~7"
```

---

## Phase 3: 代码质量 — 拆分上帝文件

### Task 5: 拆分 context_pack.py (1930行 → 4个文件)

**Files:**
- Create: `backend/app/services/context_layers/__init__.py`
- Create: `backend/app/services/context_layers/proximity.py` (~300行)
- Create: `backend/app/services/context_layers/facts.py` (~350行)
- Create: `backend/app/services/context_layers/rag.py` (~250行)
- Modify: `backend/app/services/context_pack.py` → 保留 ContextPack dataclass + ContextPackBuilder (调用 layers)

**拆分原则:** 每个 layer builder 是独立函数，接收 db session + project context，返回填充好的 pack 字段。

- [ ] **Step 1: 创建 context_layers 包**

```python
# backend/app/services/context_layers/__init__.py
from app.services.context_layers.proximity import build_proximity_layer
from app.services.context_layers.facts import build_facts_layer
from app.services.context_layers.rag import build_rag_layer

__all__ = ["build_proximity_layer", "build_facts_layer", "build_rag_layer"]
```

- [ ] **Step 2: 提取 _build_proximity → proximity.py**

将 `context_pack.py:830-980` 的 `_build_proximity` 方法提取为独立函数：

```python
# backend/app/services/context_layers/proximity.py
"""Layer 1: Proximity Layer — recent chapters + current outline."""

from __future__ import annotations
import logging
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.project import Chapter, Outline, Volume, VolumeSummary

logger = logging.getLogger(__name__)

async def build_proximity_layer(
    pack,  # ContextPack
    project_id: str | UUID,
    volume_id: str | UUID,
    chapter_idx: int,
    db: AsyncSession,
) -> None:
    """Populate pack with proximity context (last N chapters, current outline)."""
    pid = str(project_id)
    vid = str(volume_id)
    # ... (move body of _build_proximity here)
```

- [ ] **Step 3: 提取 _build_facts → facts.py**

将 `context_pack.py:983-1250` 提取。

- [ ] **Step 4: 提取 _build_rag → rag.py**

将 `context_pack.py:1347-1500` 提取。

- [ ] **Step 5: 更新 ContextPackBuilder.build() 调用新模块**

```python
# context_pack.py (simplified)
from app.services.context_layers import build_proximity_layer, build_facts_layer, build_rag_layer

class ContextPackBuilder:
    async def build(self, project_id, volume_id, chapter_idx, db=None):
        # ... setup code ...
        await build_proximity_layer(pack, project_id, volume_id, chapter_idx, await self._get_db())
        await build_facts_layer(pack, project_id, chapter_idx, global_chapter_idx, await self._get_db())
        await build_rag_layer(pack, project_id, chapter_idx, await self._get_db())
        # ... post-processing ...
        return pack
```

- [ ] **Step 6: 运行全量测试确保不 break**

```bash
cd /root/ai-write/backend && python -m pytest --tb=short -q
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/context_layers/ backend/app/services/context_pack.py
git commit -m "refactor: split context_pack.py into layer modules (proximity/facts/rag)"
```

---

### Task 6: 拆分 knowledge_tasks.py (2370行 → 按 task type 拆分)

**Files:**
- Create: `backend/app/tasks/generation_tasks.py` — run_async_generation + pipeline
- Create: `backend/app/tasks/book_tasks.py` — vectorize_book, process_uploaded_book, crawl_book
- Create: `backend/app/tasks/analysis_tasks.py` — extract_features, run_quality_score, batch_test
- Modify: `backend/app/tasks/knowledge_tasks.py` → 保留公共 helpers + re-export
- Modify: `backend/app/tasks/__init__.py` — 更新 celery task 注册

- [ ] **Step 1: 创建 generation_tasks.py**

提取 `run_async_generation` (line 389-1811) 和 `run_pipeline_generation` (line 1812-1916)。

- [ ] **Step 2: 创建 book_tasks.py**

提取 `process_uploaded_book` (line 1917-2045) 和 `crawl_book` (line 2119-2237)。

- [ ] **Step 3: 创建 analysis_tasks.py**

提取 `extract_features` (line 2238-2327) 和 `run_quality_score` (line 2328-2370)。

- [ ] **Step 4: knowledge_tasks.py 保留公共 helpers + 向后兼容 re-export**

```python
# 保留: _single_shot_llm_timeout_kwargs, _stage_needs_review_chapter_text,
#        _resolve_task_chapter_target_words, _run_async, _make_session
# Re-export for backward compatibility:
from app.tasks.generation_tasks import run_async_generation, run_pipeline_generation
from app.tasks.book_tasks import process_uploaded_book, crawl_book, vectorize_book_task
from app.tasks.analysis_tasks import extract_features, run_quality_score
```

- [ ] **Step 5: 更新 __init__.py celery app 注册**

- [ ] **Step 6: 运行测试**

```bash
cd /root/ai-write/backend && python -m pytest --tb=short -q
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/tasks/
git commit -m "refactor: split knowledge_tasks.py into generation/book/analysis modules"
```

---

## Phase 4: 性能优化 — context_pack 查询合并

### Task 7: 添加 eager loading 消除 N+1 查询

**Files:**
- Modify: `backend/app/services/context_layers/proximity.py`
- Modify: `backend/app/services/context_layers/facts.py`
- Create: `backend/tests/services/test_context_pack_queries.py`

**问题:** 单次 build() 触发 21+ 次独立 DB roundtrip，零 eager loading。

**目标:** 合并为 5-7 次 roundtrip，使用 selectinload 预加载关联。

- [ ] **Step 1: 写测试验证查询次数**

```python
# backend/tests/services/test_context_pack_queries.py
import pytest
from unittest.mock import patch, AsyncMock

@pytest.mark.asyncio
async def test_build_query_count_reduced():
    """build() should issue no more than 8 DB roundtrips."""
    # Count execute calls on the session
    ...
```

- [ ] **Step 2: 合并 proximity layer 查询**

当前分散的查询：
1. `select(Volume.volume_idx)` — 获取 volume 位置
2. `select(Chapter.summary, Chapter.chapter_idx)` — 最近章节摘要
3. `select(Chapter)` — 当前章节全文
4. `select(Chapter.chapter_idx, Chapter.title, Chapter.outline_json)` — 章节标题
5. `select(Outline.content_json)` — 大纲内容 (3次)
6. `select(VolumeSummary.summary_text)` — 卷摘要

合并为 2 次查询：
- 一次查 Volume + 关联 Chapters (selectinload)
- 一次查 Outlines (batch by volume_id)

```python
from sqlalchemy.orm import selectinload

# 合并: 一次拿到 volume + 所有 chapters
stmt = (
    select(Volume)
    .where(Volume.id == vid)
    .options(selectinload(Volume.chapters))
)
volume = (await db.execute(stmt)).scalar_one_or_none()
```

- [ ] **Step 3: 合并 facts layer 查询**

当前分散：
1. `select(WorldRule)` — 世界规则
2. `select(Character)` — 角色卡
3. `select(Foreshadow)` — 伏笔
4. `select(Chapter.chapter_idx, Chapter.summary)` — 时间线

合并为 2 次查询：
- `select(WorldRule, Character)` 分别 (无法合并不同表)
- 但可以使用 `asyncio.gather` 并行发起

```python
import asyncio

world_rules_task = db.execute(select(WorldRule).where(WorldRule.project_id == pid))
characters_task = db.execute(select(Character).where(Character.project_id == pid))
foreshadows_task = db.execute(select(Foreshadow).where(Foreshadow.project_id == pid))

world_rules_r, characters_r, foreshadows_r = await asyncio.gather(
    world_rules_task, characters_task, foreshadows_task
)
```

- [ ] **Step 4: 运行测试验证**

```bash
cd /root/ai-write/backend && python -m pytest tests/services/test_context_pack_queries.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/context_layers/ backend/tests/services/test_context_pack_queries.py
git commit -m "perf: reduce context_pack DB roundtrips from 21+ to ~7 via eager loading + gather"
```

---

## Phase 5: 可维护性 — 配置集中化

### Task 8: 收归散落 os.getenv 到 Settings

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/tasks/knowledge_tasks.py` (12处)
- Modify: `backend/app/services/reference_ingestor.py` (6处)
- Modify: `backend/app/api/generate.py` (4处)
- Modify: `backend/app/services/chapter_pipeline.py` (2处)
- Modify: `backend/app/services/scene_orchestrator.py` (3处)

- [ ] **Step 1: 扩展 Settings 类**

```python
# backend/app/config.py — 新增字段
class Settings(BaseSettings):
    # ... existing fields ...

    # --- Generation ---
    SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS: float = 840.0
    SINGLE_SHOT_LLM_RETRY_ATTEMPTS: int = 1
    SINGLE_SHOT_LLM_STREAM: bool = False
    SINGLE_SHOT_LLM_BUDGET_ENDPOINTS: int = 2
    SINGLE_SHOT_FALLBACK_TIMEOUT_SECONDS: float = 420.0
    FORCE_DIRECT_CHAPTER: bool = False
    SCENE_MODE_TIMEOUT_HARD_CAP_SECONDS: float = 600.0
    CHAPTER_QUALITY_GATE_TIMEOUT_SECONDS: float = 420.0
    CHAPTER_PIPELINE_ENABLED: bool = True
    LOGIC_CRITIC_MAX_ROUNDS: int = 2
    CHAPTER_MAX_REWRITE_ROUNDS: int = 2

    # --- Ingestor ---
    REFERENCE_INGEST_CONCURRENCY: int = 20
    DECOMPILE_RETRY_LOCK_TTL: int = 10800
    DECOMPILE_RETRY_FAST_DELAY: int = 30
    DECOMPILE_RETRY_STALL_DELAY: int = 60

    # --- Auth ---
    AUTH_USERNAME: str = "king"
    AUTH_PASSWORD_HASH: str = ""
    DISABLE_AUTH: bool = False

    # --- Feature flags ---
    CONTEXT_PACK_V2_ENABLED: bool = True
    STYLE_REDACTION_ENABLED: bool = True
    QUALITY_GATE_PERSIST_ON_BLOCK: bool = True
```

- [ ] **Step 2: 替换 knowledge_tasks.py 中的 os.getenv**

```python
# Before:
configured = float(_os.getenv("SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS", "840"))

# After:
from app.config import settings
configured = settings.SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS
```

对 12 处逐一替换。

- [ ] **Step 3: 替换其他文件中的 os.getenv**

对 reference_ingestor.py (6处)、generate.py (4处)、chapter_pipeline.py (2处)、scene_orchestrator.py (3处) 逐一替换。

- [ ] **Step 4: 替换 auth.py 中的 os.environ.get**

```python
# Before:
_USERNAME = os.environ.get("AUTH_USERNAME", "king")
_PASSWORD_HASH = os.environ.get("AUTH_PASSWORD_HASH", "...")

# After:
from app.config import settings
_USERNAME = settings.AUTH_USERNAME
_PASSWORD_HASH = settings.AUTH_PASSWORD_HASH
```

- [ ] **Step 5: 运行测试**

```bash
cd /root/ai-write/backend && python -m pytest --tb=short -q
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/config.py backend/app/tasks/ backend/app/services/ backend/app/api/
git commit -m "refactor: centralize 61 scattered os.getenv calls into Settings class"
```

---

### Task 9: 精简 broad exception handling

**Files:**
- Modify: `backend/app/services/context_pack.py`

**问题:** 35 处 `except Exception` 吞掉所有错误。需区分"可降级"和"必须失败"。

**原则:**
- Layer 构建失败 = 可降级（log warning，返回空数据，继续生成）
- DB 连接失败 = 必须失败（向上抛，让调用方处理）
- 缓存检查失败 = 可降级

- [ ] **Step 1: 分类现有 35 处 exception**

分为 3 类：
- A类: 可降级辅助功能（缓存、style injection、naming directive）→ 保留 except Exception + warning
- B类: 核心数据加载（chapters, outlines, characters）→ 改为捕获具体异常或向上抛
- C类: 已经是正确的降级处理 → 保留

- [ ] **Step 2: 修改 B类 — 核心数据加载异常**

对 `_build_proximity` 和 `_build_facts` 中的核心查询，改为：
```python
# Before:
try:
    result = await db.execute(select(Chapter)...)
except Exception as e:
    logger.warning("...", e)
    return  # silently returns empty

# After (核心数据):
result = await db.execute(select(Chapter)...)  # let it propagate
# OR for semi-critical:
from sqlalchemy.exc import SQLAlchemyError
try:
    result = await db.execute(select(Chapter)...)
except SQLAlchemyError as e:
    logger.error("Critical: cannot load chapters for context: %s", e)
    raise  # caller decides how to handle
```

- [ ] **Step 3: A类保留但改善日志**

```python
# 确保每个 warning 包含 project_id 和 chapter_idx 上下文
logger.warning(
    "Style v9 injection skipped (project=%s, ch=%d): %s",
    project_id, chapter_idx, exc,
)
```

- [ ] **Step 4: 运行测试**

```bash
cd /root/ai-write/backend && python -m pytest --tb=short -q
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/context_pack.py
git commit -m "refactor: differentiate degradable vs critical exceptions in context_pack"
```

---

## Phase 5: 前端完善

### Task 9: 前端测试框架搭建 + 核心模块覆盖

**Files:**
- Create: `frontend/vitest.config.ts`
- Create: `frontend/src/__tests__/api.test.ts`
- Create: `frontend/src/__tests__/projectStore.test.ts`
- Modify: `frontend/package.json`
- Modify: `frontend/tsconfig.json`

- [ ] **Step 1: 安装测试依赖**

```bash
cd /root/ai-write/frontend
npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom @vitejs/plugin-react
```

- [ ] **Step 2: 创建 vitest 配置**

```typescript
// frontend/vitest.config.ts
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/__tests__/setup.ts'],
    globals: true,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
})
```

- [ ] **Step 3: 创建 test setup**

```typescript
// frontend/src/__tests__/setup.ts
import '@testing-library/jest-dom'
```

- [ ] **Step 4: 为 api.ts 写测试**

```typescript
// frontend/src/__tests__/api.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock fetch globally
const mockFetch = vi.fn()
global.fetch = mockFetch

describe('apiFetch', () => {
  beforeEach(() => {
    mockFetch.mockReset()
    // Clear localStorage
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => 'test-token'),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    })
  })

  it('adds Authorization header from localStorage', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ data: 'test' }),
    })

    const { apiFetch } = await import('@/lib/api')
    await apiFetch('/api/projects')

    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/projects'),
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer test-token',
        }),
      }),
    )
  })

  it('throws on non-ok response', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 401,
      json: () => Promise.resolve({ detail: 'Unauthorized' }),
    })

    const { apiFetch } = await import('@/lib/api')
    await expect(apiFetch('/api/projects')).rejects.toThrow()
  })
})
```

- [ ] **Step 5: 为 projectStore 写测试**

```typescript
// frontend/src/__tests__/projectStore.test.ts
import { describe, it, expect, beforeEach } from 'vitest'
import { useProjectStore } from '@/stores/projectStore'

describe('projectStore', () => {
  beforeEach(() => {
    useProjectStore.setState({
      projects: [],
      currentProject: null,
      volumes: [],
      chapters: [],
    })
  })

  it('setProjects updates project list', () => {
    const projects = [{ id: '1', title: 'Test Novel', genre: 'fantasy' }]
    useProjectStore.getState().setProjects(projects as any)
    expect(useProjectStore.getState().projects).toHaveLength(1)
    expect(useProjectStore.getState().projects[0].title).toBe('Test Novel')
  })

  it('selectProject sets currentProject', () => {
    const project = { id: '1', title: 'Test', genre: 'xianxia' }
    useProjectStore.getState().setCurrentProject(project as any)
    expect(useProjectStore.getState().currentProject?.id).toBe('1')
  })
})
```

- [ ] **Step 6: 添加 test script 到 package.json**

在 `frontend/package.json` 的 scripts 中添加:
```json
"test": "vitest",
"test:run": "vitest run"
```

- [ ] **Step 7: 运行测试验证**

```bash
cd /root/ai-write/frontend && npm run test:run
```

Expected: 所有测试通过

- [ ] **Step 8: Commit**

```bash
git add frontend/vitest.config.ts frontend/src/__tests__/ frontend/package.json frontend/package-lock.json
git commit -m "test(frontend): add Vitest framework + api/projectStore unit tests"
```

---

### Task 10: 消除前端 `any` 类型 (Top 20)

**Files:**
- Modify: `frontend/src/components/workspace/DesktopWorkspace.tsx`
- Modify: `frontend/src/components/panels/GeneratePanel.tsx`
- Modify: `frontend/src/stores/projectStore.ts`

- [ ] **Step 1: 查找 any 使用热点**

```bash
cd /root/ai-write/frontend
grep -rn ": any\|as any\|<any>" src/ --include="*.ts" --include="*.tsx" | \
  sed 's/:.*//' | sort | uniq -c | sort -rn | head -10
```

- [ ] **Step 2: 为 API 响应定义共享类型**

在 `frontend/src/lib/types.ts` 中定义后端响应对应的 TypeScript 接口:

```typescript
// frontend/src/lib/types.ts
export interface Project {
  id: string
  title: string
  genre: string
  target_word_count: number
  created_at: string
  updated_at: string
  deleted_at: string | null
}

export interface Volume {
  id: string
  project_id: string
  title: string
  volume_idx: number
  summary: string | null
}

export interface Chapter {
  id: string
  volume_id: string
  title: string
  chapter_idx: number
  content_text: string
  word_count: number
  status: string
  summary: string | null
  outline_json: Record<string, unknown> | null
  target_word_count: number | null
}

export interface Outline {
  id: string
  project_id: string
  level: 'book' | 'volume' | 'chapter'
  content_json: Record<string, unknown>
}

export interface GenerationTask {
  id: string
  project_id: string
  chapter_id: string | null
  task_type: string
  status: 'pending' | 'running' | 'done' | 'failed'
  result_text: string | null
  error_message: string | null
  created_at: string
}
```

- [ ] **Step 3: 替换 store 中的 any**

将 `projectStore.ts` 中的 `any` 替换为上面定义的类型。

- [ ] **Step 4: TypeScript 编译检查**

```bash
cd /root/ai-write/frontend && npx tsc --noEmit 2>&1 | head -30
```

Expected: 无新增错误

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/stores/projectStore.ts
git commit -m "types(frontend): add shared API response types, eliminate any in projectStore"
```

---

### Task 11: 全局 Error Boundary + 表单验证

**Files:**
- Create: `frontend/src/components/ErrorBoundary.tsx`
- Modify: `frontend/src/app/layout.tsx`
- Modify: `frontend/src/components/panels/GeneratePanel.tsx`

- [ ] **Step 1: 创建全局 Error Boundary**

```tsx
// frontend/src/components/ErrorBoundary.tsx
'use client'

import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  fallback?: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('[ErrorBoundary]', error, info.componentStack)
  }

  render() {
    if (this.state.hasError) {
      return this.props.fallback ?? (
        <div className="flex flex-col items-center justify-center min-h-[50vh] gap-4">
          <div className="text-danger-500 text-lg font-medium">页面出现错误</div>
          <p className="text-sm text-gray-500 max-w-md text-center">
            {this.state.error?.message || '未知错误'}
          </p>
          <button
            onClick={() => this.setState({ hasError: false, error: null })}
            className="px-4 py-2 bg-brand-500 text-white rounded-lg hover:bg-brand-600 transition-colors"
          >
            重试
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
```

- [ ] **Step 2: 在 layout.tsx 中包裹**

在 `frontend/src/app/layout.tsx` 的 `<body>` 内层添加 `<ErrorBoundary>`:

```tsx
import { ErrorBoundary } from '@/components/ErrorBoundary'

// ... in the return:
<body>
  <ErrorBoundary>
    {children}
  </ErrorBoundary>
</body>
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ErrorBoundary.tsx frontend/src/app/layout.tsx
git commit -m "feat(frontend): add global ErrorBoundary with retry button"
```

---

### Task 12: CORS 环境变量化

**Files:**
- Modify: `backend/app/main.py:225-234`
- Modify: `backend/app/config.py`
- Modify: `.env.example`

- [ ] **Step 1: 添加 CORS_ORIGINS 到 Settings**

```python
# backend/app/config.py — 在 Settings 类中添加
    # --- CORS ---
    CORS_ORIGINS: list[str] = ["http://localhost:3100", "http://localhost:8080"]
```

- [ ] **Step 2: 修改 main.py 使用 Settings**

```python
# backend/app/main.py 替换 CORS 中间件配置
from app.config import settings

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

- [ ] **Step 3: 更新 .env.example**

```bash
# CORS (逗号分隔多个 origin)
CORS_ORIGINS=["http://localhost:3100","http://localhost:8080"]
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/config.py backend/app/main.py .env.example
git commit -m "config: move CORS origins from hardcoded to Settings"
```

---

## 执行顺序建议

| 优先级 | Phase | Task | 预估时间 | 风险 |
|--------|-------|------|----------|------|
| P0 | 1 | Task 1: .env.example 清理 | 5 min | 无 |
| P0 | 1 | Task 12: CORS 环境变量化 | 10 min | 低 |
| P1 | 2 | Task 2: context_pack 查询优化 | 30 min | 中 — 需验证查询结果一致 |
| P1 | 3 | Task 3: knowledge_tasks 拆分 | 45 min | 中 — 需确认 Celery task 注册 |
| P1 | 3 | Task 4: context_pack 模块拆分 | 60 min | 高 — 核心模块，需回归测试 |
| P2 | 4 | Task 5: 配置集中化 | 20 min | 低 |
| P2 | 4 | Task 6: 异常分级 | 30 min | 低 |
| P2 | 5 | Task 9: 前端测试框架 | 20 min | 低 |
| P2 | 5 | Task 10: 消除 any | 30 min | 低 |
| P2 | 5 | Task 11: ErrorBoundary | 15 min | 低 |

**总预估：约 4-5 小时**

建议执行路径：P0 先行（15分钟搞定），然后 P1 性能+架构（按 Task 2→3→4 顺序），最后 P2 可维护性+前端（可并行）。

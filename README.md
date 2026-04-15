# AI Write - AI-Powered Novel Writing Platform

Full-stack AI novel writing platform. AI handles everything from outlines to chapters — human only reviews and fine-tunes. Supports learning writing styles from existing novels, hierarchical memory for ultra-long novels (2-5 million characters without continuity errors), and condition-triggered foreshadow management.

## Architecture

```
                    Nginx (:80)
                   /          \
        Next.js (:3000)    FastAPI (:8000)
        Frontend            Backend
        |                   |
        |              Celery Workers
        |                   |
        +------- Storage Cluster -------+
        |           |          |        |
   PostgreSQL    Qdrant     Neo4j    Redis
   (business)   (vectors)  (graph)  (cache/queue)
```

**Tech Stack:**
- **Frontend:** Next.js 16 + TypeScript + Tailwind CSS + Zustand + ProseMirror
- **Backend:** Python 3.11+ / FastAPI + Celery + SQLAlchemy 2.0 (async)
- **Storage:** PostgreSQL 16 + Qdrant + Neo4j 5 + Redis 7
- **LLM:** Anthropic (Claude) / OpenAI (GPT) / Any OpenAI-compatible endpoint / LoRA fine-tuned local models

## Quick Start

### 1. Clone & Configure

```bash
git clone https://github.com/KingzZovo/ai-write.git
cd ai-write
cp .env.example .env
```

Edit `.env` and set at least one LLM API key:
```env
ANTHROPIC_API_KEY=sk-ant-...
# or
OPENAI_API_KEY=sk-...
# or (for local/third-party models)
OPENAI_COMPATIBLE_BASE_URL=http://localhost:8001/v1
OPENAI_COMPATIBLE_API_KEY=...
```

### 2. Start Services

```bash
docker compose up -d
```

This starts 8 services: PostgreSQL, Redis, Qdrant, Neo4j, FastAPI backend, Celery worker, Next.js frontend, Nginx.

### 3. Initialize Database

```bash
docker compose exec backend alembic upgrade head
```

### 4. Access

- **Web UI:** http://localhost
- **API Docs:** http://localhost:8000/docs
- **Neo4j Browser:** http://localhost:7474

## Core Features

### Phase 1: AI Writing Pipeline

Full AI-driven creation flow:

```
Creative Input → Book Outline → Volume Outlines → Chapter Outlines → Chapter Content
                 (AI generates)  (AI generates)    (AI generates)     (AI generates)
                    ↓                ↓                  ↓                  ↓
                 User reviews     User reviews       User reviews      User reviews
```

- **Dual-Agent Pipeline:** PlotAgent (story coherence) → StyleAgent (style polish)
- **SSE Streaming:** Real-time generation with typewriter effect
- **ModelRouter:** Unified LLM access with task-based routing, fallback chains, token tracking

### Phase 2: Knowledge Base & Style Learning

Import reference novels to learn writing styles:

- **Legado Book Source Engine:** Import book source rules from legado (阅读) app, supports CSS/XPath/JSONPath/regex/@js selectors
- **Ranking Discovery:** Browse bestseller lists via `ruleExplore`
- **Text Pipeline:** TXT/EPUB/HTML parsing → chapter detection → noise stripping → block slicing
- **Style Extraction:** jieba-based statistical analysis (sentence length, dialogue ratio, rhetoric, POV)
- **Style Clustering:** DBSCAN/KMeans → auto-generated StyleProfile configs
- **Quality Scoring:** LLM-as-judge 5-dimension evaluation, filters low-quality novels from training

### Phase 3: Memory System (200-500 万字)

5-layer hierarchical memory pyramid:

| Layer | Content | Storage | Recall |
|-------|---------|---------|--------|
| L1 World Rules | Power systems, geography, core rules | Neo4j + PG | Always full |
| L2 Volume Summary | Per-volume plot progress, character snapshots | PostgreSQL | Always full |
| L3 Chapter Summary | Per-chapter key events, characters, emotions | Qdrant | Current volume + vector search |
| L4 Recent Text | Previous + current chapter full text | PostgreSQL | Always full |
| L5 Entity Timeline | Character states, relationships over time | Neo4j | Query by chapter |

**Entity Timeline (Neo4j):**
- Track character states, relationships, locations across thousands of chapters
- Time-point snapshot queries: "What was character X's state at chapter N?"
- Auto-extraction from generated text via LLM

**Condition-Triggered Foreshadow Management:**
- No hard chapter deadlines (avoids forcing plot progression)
- `resolve_conditions` describe WHEN resolution is natural
- `narrative_proximity` (0.0-1.0) computed via LLM
- Lifecycle: planted → ripening (>0.7) → ready (>0.9) → resolved
- Auto-detection of new foreshadows and resolutions in generated text

**Hook System:**
- Pre-generate: foreshadow check, character consistency, outline alignment
- Post-generate: entity extraction → Neo4j, summary generation → Qdrant, foreshadow check

### Phase 4: Quality & Advanced Features

- **LLM-as-a-Judge:** 5-dimension chapter scoring (plot/character/style/pacing/foreshadow)
- **Version Control:** Git-like branching, diff comparison, merge
- **Batch Generation:** Multi-chapter sequential generation with hook integration
- **Semantic Cache:** Redis-backed, skips generation tasks, caches extractions/summaries
- **Text Rewriting:** Select text → condense/expand/restructure/continue/custom
- **Cascade Regeneration:** Edit impact analysis on downstream chapters
- **Token Dashboard:** Real-time usage tracking and cache hit rate

### LoRA Fine-tuning Support

Train custom style models on your RTX 5080 (16GB):

```
Cloud Server (ai-write)  ←→  Home GPU (RTX 5080)
  - Web UI + API              - Qwen2.5-7B + QLoRA training
  - All databases             - vLLM/Ollama inference
  - Anthropic/OpenAI API      - Connected via frp/Cloudflare Tunnel
```

**Workflow:**
1. Import reference novels → quality scoring → style extraction
2. `POST /api/lora/export-dataset` → Alpaca/ShareGPT training JSON
3. `POST /api/lora/generate-script` → Unsloth training script
4. Train on home GPU (~1-2 hours for 5000 samples)
5. Load adapter in Ollama/vLLM → set `OPENAI_COMPATIBLE_BASE_URL`
6. AI Write automatically uses fine-tuned model

## Project Structure

```
ai-write/
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── pyproject.toml
│   ├── alembic/                    # Database migrations
│   └── app/
│       ├── main.py                 # FastAPI entry point
│       ├── config.py               # Pydantic settings
│       ├── models/                 # 16 SQLAlchemy ORM models
│       ├── schemas/                # Pydantic request/response
│       ├── api/                    # 10 API route modules, 59 endpoints
│       │   ├── projects.py         # Project CRUD
│       │   ├── outlines.py         # Outline CRUD + confirm
│       │   ├── chapters.py         # Chapter CRUD + sync
│       │   ├── generate.py         # SSE streaming (chapter + outline)
│       │   ├── knowledge.py        # Book sources, uploads, crawling
│       │   ├── foreshadows.py      # Foreshadow CRUD + resolve
│       │   ├── settings.py         # Characters + world rules
│       │   ├── versions.py         # Version tree + diff + evaluate
│       │   ├── rewrite.py          # Text rewrite + batch generate
│       │   └── lora.py             # LoRA dataset export + training
│       ├── services/               # 22 business logic services
│       │   ├── model_router.py     # Unified LLM access (3 providers)
│       │   ├── outline_generator.py # 3-level outline generation
│       │   ├── context_assembler.py # 5-layer memory assembly
│       │   ├── chapter_generator.py # Dual-agent orchestration
│       │   ├── memory.py           # Hierarchical memory pyramid
│       │   ├── entity_timeline.py  # Neo4j knowledge graph
│       │   ├── foreshadow_manager.py # Condition-triggered foreshadows
│       │   ├── hook_manager.py     # Pre/post generation hooks
│       │   ├── book_source_engine.py # Legado rule interpreter
│       │   ├── text_pipeline.py    # Text cleaning & slicing
│       │   ├── feature_extractor.py # Plot + style extraction
│       │   ├── style_clustering.py # DBSCAN/KMeans clustering
│       │   ├── quality_scorer.py   # Novel quality evaluation
│       │   ├── chapter_evaluator.py # Chapter quality scoring
│       │   ├── version_control.py  # Git-like version management
│       │   ├── batch_generator.py  # Multi-chapter generation
│       │   ├── semantic_cache.py   # Redis LLM response cache
│       │   ├── text_rewriter.py    # Inline text operations
│       │   ├── cascade_regenerator.py # Edit impact analysis
│       │   ├── incremental_sync.py # Real-time edit sync
│       │   ├── qdrant_store.py     # Vector storage management
│       │   ├── lora_manager.py     # LoRA training data + scripts
│       │   └── agents/
│       │       ├── plot_agent.py   # Story generation agent
│       │       └── style_agent.py  # Style polishing agent
│       └── tasks/                  # Celery async tasks
│           ├── knowledge_tasks.py  # Crawling, extraction, scoring
│           └── style_tasks.py      # Periodic style clustering
├── frontend/
│   └── src/
│       ├── app/
│       │   ├── page.tsx            # Landing page
│       │   ├── workspace/page.tsx  # Main workspace
│       │   └── knowledge/page.tsx  # Knowledge management
│       ├── components/
│       │   ├── editor/             # ProseMirror editor + rewrite menu
│       │   ├── outline/            # Outline tree navigation
│       │   ├── workspace/          # Three-column layout
│       │   └── panels/             # 7 sidebar panels
│       ├── stores/                 # Zustand state management
│       └── lib/                    # API client + sync manager
└── nginx/nginx.conf
```

## Database Schema

16 tables across PostgreSQL:

| Table | Purpose |
|-------|---------|
| `projects` | Novel projects with genre, premise, settings |
| `volumes` | Book volumes with ordering |
| `chapters` | Chapter content, outlines, word count, status |
| `outlines` | Hierarchical outlines (book/volume/chapter) |
| `characters` | Character profiles (synced with Neo4j) |
| `world_rules` | World-building rules and constraints |
| `foreshadows` | Foreshadow tracking with conditions and proximity |
| `style_profiles` | Extracted style configurations |
| `model_configs` | Per-task LLM model assignments |
| `volume_summaries` | Per-volume plot summaries for memory |
| `book_sources` | Legado-compatible book source rules |
| `reference_books` | Imported novels for style learning |
| `text_chunks` | Sliced text blocks from reference books |
| `crawl_tasks` | Novel crawling job tracking |
| `chapter_versions` | Version tree with diff storage |
| `chapter_evaluations` | Quality evaluation scores |

## API Reference

**59 endpoints** across 10 route modules. Full OpenAPI docs at `/docs`.

| Module | Prefix | Key Endpoints |
|--------|--------|---------------|
| Projects | `/api/projects` | CRUD |
| Outlines | `/api/projects/{id}/outlines` | CRUD + confirm |
| Chapters | `/api/projects/{id}/chapters` | CRUD + sync |
| Generate | `/api/generate` | SSE chapter + outline generation |
| Knowledge | `/api/knowledge` | Sources, books, upload, crawl, explore |
| Foreshadows | `/api/projects/{id}/foreshadows` | CRUD + resolve |
| Settings | `/api/projects/{id}` | Characters + world rules CRUD |
| Versions | `/api/chapters/{id}/versions` | Tree, diff, branch, evaluate |
| Rewrite | `/api/rewrite` | Text operations + batch generate |
| LoRA | `/api/lora` | Dataset export, training scripts, adapters |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | One of these | Anthropic API key (Claude) |
| `OPENAI_API_KEY` | required | OpenAI API key (GPT) |
| `OPENAI_COMPATIBLE_BASE_URL` | | Third-party/local model endpoint |
| `DATABASE_URL` | Auto | PostgreSQL connection string |
| `REDIS_URL` | Auto | Redis connection string |
| `QDRANT_HOST` | Auto | Qdrant hostname |
| `NEO4J_URI` | Auto | Neo4j bolt URI |
| `NEO4J_PASSWORD` | Auto | Neo4j password |
| `SECRET_KEY` | Yes | Application secret key |

## Development

### Local Backend (without Docker)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Start PostgreSQL and Redis separately, then:
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/aiwrite
export REDIS_URL=redis://localhost:6379/0
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

### Local Frontend

```bash
cd frontend
npm install
npm run dev
```

### Run Celery Worker

```bash
cd backend && source .venv/bin/activate
celery -A app.tasks:celery_app worker --loglevel=info
```

### Run Celery Beat (periodic tasks)

```bash
celery -A app.tasks:celery_app beat --loglevel=info
```

## Roadmap

See **[ITERATION_PLAN.md](ITERATION_PLAN.md)** for the full 7-iteration development plan.

**Next up:**
1. **Iteration 1:** E2E validation with real LLM APIs — generate a 5-chapter story end-to-end
2. **Iteration 2:** Test suite (367 test gaps) + GitHub Actions CI
3. **Iteration 3:** Frontend polish (ProseMirror deep integration, generation wizard, Chinese UI)
4. **Iteration 4:** Robustness at scale (100+ chapters, WebSocket notifications)
5. **Iteration 5:** LoRA training UI + multi-style support
6. **Iteration 6:** Authentication + security hardening
7. **Iteration 7:** Export (EPUB/PDF/DOCX) + publishing

## License

MIT

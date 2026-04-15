# AI Write Iteration Plan

## Current Status (v0.1.0)

All 4 Phases implemented, code reviewed, E2E tested against PostgreSQL + Redis.

**What works:**
- Full API (59 endpoints, all tested)
- Database schema (16 tables, Alembic migration applied)
- FastAPI server starts and serves requests
- Frontend builds and renders
- Text pipeline (TXT/EPUB/HTML parsing, chapter detection, slicing)
- Style extraction (jieba statistical analysis)
- Book source rule engine (CSS/XPath/JSON/regex parser)
- Docker Compose orchestration

**What has NOT been E2E tested (needs real LLM API keys):**
- Actual AI generation (outline + chapter) via Anthropic/OpenAI
- Dual-agent pipeline (PlotAgent → StyleAgent)
- Feature extraction via LLM (plot extractor)
- Quality scoring/evaluation via LLM
- Foreshadow proximity calculation via LLM
- Entity extraction via LLM → Neo4j
- Embedding generation → Qdrant storage

---

## Iteration 1: E2E Validation & Bug Fixes

**Goal:** Connect real LLM APIs, run full generation pipeline end-to-end, fix runtime bugs.

**Tasks:**
- [ ] Configure `.env` with real API keys
- [ ] Test full flow: create project → generate book outline → volume outline → chapter outline → chapter content
- [ ] Verify SSE streaming works in browser
- [ ] Test book source import + crawling against a real novel site
- [ ] Test file upload (TXT/EPUB) → cleaning → chunk storage
- [ ] Test quality scoring on imported novel
- [ ] Fix any runtime errors discovered during testing
- [ ] Test Neo4j entity extraction after chapter generation
- [ ] Test Qdrant embedding storage/retrieval
- [ ] Verify Celery worker processes async tasks correctly

**Success criteria:** Generate a 5-chapter short story end-to-end without manual intervention.

---

## Iteration 2: Test Suite & CI

**Goal:** Address the 367 test gaps identified by code-review-graph.

**Tasks:**
- [ ] Set up pytest + pytest-asyncio for backend
- [ ] Unit tests for core services:
  - [ ] `text_pipeline.py` (parsing, chapter detection, slicing)
  - [ ] `feature_extractor.py` (style extraction)
  - [ ] `book_source_engine.py` (rule parsing)
  - [ ] `context_assembler.py` (memory assembly)
  - [ ] `version_control.py` (versioning, diff, merge)
  - [ ] `foreshadow_manager.py` (proximity, lifecycle)
  - [ ] `model_router.py` (provider selection, fallback)
- [ ] Integration tests for API endpoints (use test DB)
- [ ] Frontend: Jest + React Testing Library for key components
- [ ] GitHub Actions CI pipeline (lint + test on push)
- [ ] Pre-commit hooks (ruff lint + type check)

---

## Iteration 3: Frontend Polish

**Goal:** Make the UI production-ready.

**Tasks:**
- [ ] ProseMirror deep integration:
  - [ ] AI-generated text marks (colored background)
  - [ ] Selection → floating rewrite menu (connect RewriteMenu component)
  - [ ] Streaming typewriter effect with proper cursor tracking
- [ ] Outline editor:
  - [ ] Visual outline tree with drag-and-drop reordering
  - [ ] Inline editing of outline content
  - [ ] Step-by-step generation wizard (book → volume → chapter → content)
- [ ] Generation flow UX:
  - [ ] Progress indicators for each generation step
  - [ ] Error recovery (retry failed generations)
  - [ ] "Regenerate" button per section
- [ ] Knowledge page:
  - [ ] Drag-and-drop file upload
  - [ ] Real-time crawl progress with WebSocket
  - [ ] Style profile preview with visual charts
- [ ] Responsive layout for tablet
- [ ] Chinese localization (all UI text)
- [ ] Dark mode support

---

## Iteration 4: Robustness & Performance

**Goal:** Handle long novels reliably.

**Tasks:**
- [ ] Stress test with 100+ chapters:
  - [ ] Memory recall accuracy at scale
  - [ ] Neo4j query performance with 1000+ entities
  - [ ] Qdrant search latency with 10000+ vectors
- [ ] WebSocket notifications for:
  - [ ] Incremental sync completion
  - [ ] Crawl task progress
  - [ ] Background generation completion
- [ ] Batch generation improvements:
  - [ ] Real SSE progress per chapter (fix I6 from review)
  - [ ] Pause/resume persistence across server restarts
  - [ ] Failure retry with exponential backoff
- [ ] Token budget optimization:
  - [ ] Dynamic budget allocation based on model context window
  - [ ] Smarter truncation with semantic importance ranking
- [ ] Database optimization:
  - [ ] Add indexes for frequent query patterns
  - [ ] Connection pool tuning
  - [ ] Query performance profiling

---

## Iteration 5: LoRA Training Integration

**Goal:** Seamless style fine-tuning from the web UI.

**Tasks:**
- [ ] LoRA management UI page:
  - [ ] Dataset export wizard (select books → configure → export)
  - [ ] Training job monitoring (connect to remote GPU via WebSocket)
  - [ ] Adapter browser (list, preview, activate)
  - [ ] A/B comparison (original model vs fine-tuned)
- [ ] vLLM/Ollama auto-detection:
  - [ ] Health check for `OPENAI_COMPATIBLE_BASE_URL`
  - [ ] Model list from remote endpoint
  - [ ] Auto-switch between cloud API and local model
- [ ] Training data quality:
  - [ ] Data augmentation (context variations)
  - [ ] Deduplication
  - [ ] Balance check (dialogue vs narration ratio)
- [ ] Multi-style support:
  - [ ] Train separate LoRA per style
  - [ ] Runtime style switching in generation settings
  - [ ] Style blending (merge LoRA weights)

---

## Iteration 6: Authentication & Multi-user

**Goal:** Secure deployment, optional multi-user support.

**Tasks:**
- [ ] Single-user auth (API key or session-based)
- [ ] CORS lock-down (remove allow_origins=["*"])
- [ ] Rate limiting for LLM endpoints
- [ ] HTTPS/SSL configuration
- [ ] Optional: Multi-user with separate project isolation
- [ ] Optional: Usage quotas and billing integration

---

## Iteration 7: Export & Publishing

**Goal:** Get novels out of the platform.

**Tasks:**
- [ ] Export to formats:
  - [ ] TXT (plain text with chapter headings)
  - [ ] EPUB (with cover, TOC, metadata)
  - [ ] PDF (with typography)
  - [ ] Word (.docx)
- [ ] Publishing integration:
  - [ ] One-click export to legado-compatible format
  - [ ] Metadata editing (title, author, synopsis, cover)
- [ ] Statistics dashboard:
  - [ ] Word count trends over time
  - [ ] Generation quality scores over time
  - [ ] Cost tracking per project

---

## Version History

| Version | Date | Milestone |
|---------|------|-----------|
| v0.1.0 | 2026-04-15 | Initial release: all 4 phases + LoRA support |
| v0.2.0 | TBD | Iteration 1: E2E validation complete |
| v0.3.0 | TBD | Iteration 2: Test suite + CI |
| v1.0.0 | TBD | Iteration 3-4: Production-ready |

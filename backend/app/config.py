"""Application configuration loaded from environment variables and .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for the AI Write backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Database ---
    DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/aiwrite"
    )

    # --- Redis ---
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- Qdrant ---
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333

    # --- Neo4j ---
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "neo4j"

    # --- LLM API keys (optional) ---
    ANTHROPIC_API_KEY: str | None = None
    OPENAI_API_KEY: str | None = None
    OPENAI_COMPATIBLE_BASE_URL: str | None = None
    OPENAI_COMPATIBLE_API_KEY: str | None = None

    # --- Security ---
    SECRET_KEY: str = "change-me-in-production"

    # --- CORS ---
    CORS_ORIGINS: str = "http://localhost:3100,http://localhost:8080"

    # --- Generation ---
    SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS: float = 840.0
    SYNC_SINGLE_SHOT_LLM_REQUEST_TIMEOUT_SECONDS: float = 840.0
    SYNC_SINGLE_SHOT_LLM_RETRY_ATTEMPTS: int = 1
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

    # --- LLM relay workarounds ---
    # Comma-separated model-name substrings whose relay channel drops the
    # system role (e.g. "claude-"). For matching models, system content is
    # folded into the first user message. Empty = disabled.
    LLM_MERGE_SYSTEM_INTO_USER_MODELS: str = ""
    # Comma-separated model-name substrings whose relay burns the whole output
    # budget on hidden thinking for NON-streaming completions, returning empty
    # text with output_tokens == max_tokens (observed 2026-07-26 on relay
    # claude-*; streaming the same request works). Matching models route
    # non-stream generate() through internal streaming and assemble the full
    # text before returning. Empty = disabled.
    LLM_FORCE_INTERNAL_STREAM_MODELS: str = "claude-"

    # --- Scene Planner ---
    ALLOW_SCENE_PLANNER_FALLBACK: bool = True
    SCENE_PLANNER_TIMEOUT_SECONDS: float = 180.0
    SCENE_PLANNER_TIMEOUT_HARD_CAP_SECONDS: float = 240.0

    # --- Context Pack ---
    # Total token budget for ContextPack.to_system_prompt (split 40/33/20/7
    # across L1/L2/L3/meta). Raise for bigger-context models.
    CONTEXT_PACK_TOKEN_BUDGET: int = 9500
    # Max full character cards rendered in Layer 2 / Layer-0 roster. Larger
    # casts are trimmed to chapter-relevant characters + top protagonists;
    # remaining relevant names are listed name-only in the roster.
    CONTEXT_PACK_MAX_CHARACTER_CARDS: int = 12

    # --- Memory compaction (500万字 scaling) ---
    # Auto-compaction fires (fire-and-forget, never blocks the chapter save)
    # after a chapter-summary upsert when the project's live (non-compacted)
    # chapter-summary point count exceeds this threshold.
    MEMORY_COMPACT_THRESHOLD_POINTS: int = 200

    # --- Memory pyramid (coarse-to-fine tiers) ---
    # Tier 2: max memory cards kept per (project, character). first_appearance
    # cards are always kept; oldest key_moment cards are evicted beyond cap.
    MEMORY_CARDS_PER_CHARACTER: int = 10
    # Tier 3: a chapter-relevant character absent for MORE than this many
    # chapters (gap = current global_idx - last_seen > GAP) triggers the
    # 旧人重现 drill-down block in ContextPack L3.
    MEMORY_DRILLDOWN_GAP_CHAPTERS: int = 20
    # Tier 4: chunk chapter full text into the per-project chapter_chunks
    # shard on persist (write side) / recall chunks into ContextPack L3
    # (read side).
    CHAPTER_CHUNKING_ENABLED: bool = True
    CHAPTER_CHUNK_RECALL_ENABLED: bool = True

    # --- DB scaling / retention (v1.11 partitioning groundwork) ---
    # Drop llm_call_logs monthly partitions older than this many months.
    # <= 0 disables dropping (keep forever).
    LLM_LOG_RETENTION_MONTHS: int = 6
    # How many future monthly partitions tasks.maintain_llm_log_partitions
    # keeps pre-created ahead of the current month.
    LLM_LOG_PARTITION_PRECREATE_MONTHS: int = 3
    # Keep only the newest K chapter_versions rows per chapter.
    # 0 (default) = keep all history; the active version is never deleted.
    CHAPTER_VERSION_KEEP_LAST: int = 0

    # --- Reference Ingestor ---
    STYLE_REDACTION_ENABLED: bool = True
    REFERENCE_INGEST_CONCURRENCY: int = 3
    DECOMPILE_MAX_AUTO_RETRIES: int = 5
    DECOMPILE_RETRY_INITIAL_DELAY: int = 300
    DECOMPILE_RETRY_BACKOFF_FACTOR: float = 2.0
    DECOMPILE_RETRY_WAVE_BATCH: int = 50


settings = Settings()

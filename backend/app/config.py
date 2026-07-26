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

    # --- Scene Planner ---
    ALLOW_SCENE_PLANNER_FALLBACK: bool = True
    SCENE_PLANNER_TIMEOUT_SECONDS: float = 180.0
    SCENE_PLANNER_TIMEOUT_HARD_CAP_SECONDS: float = 240.0

    # --- Reference Ingestor ---
    STYLE_REDACTION_ENABLED: bool = True
    REFERENCE_INGEST_CONCURRENCY: int = 3
    DECOMPILE_MAX_AUTO_RETRIES: int = 5
    DECOMPILE_RETRY_INITIAL_DELAY: int = 300
    DECOMPILE_RETRY_BACKOFF_FACTOR: float = 2.0
    DECOMPILE_RETRY_WAVE_BATCH: int = 50


settings = Settings()

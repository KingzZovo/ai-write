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

    # --- CORS ---
    CORS_ORIGINS: str = "http://localhost:3100,http://localhost:8080"

    # --- Security ---
    SECRET_KEY: str = "change-me-in-production"
    AUTH_USERNAME: str = "king"
    AUTH_PASSWORD_HASH: str = ""
    DISABLE_AUTH: bool = False

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
    CHAPTER_MAX_REWRITE_ROUNDS: int = 5
    QUALITY_GATE_PERSIST_ON_BLOCK: bool = False

    # --- Reference Ingestor ---
    REFERENCE_INGEST_CONCURRENCY: int = 3
    STYLE_REDACTION_ENABLED: bool = True
    SEMANTIC_CHUNKER_MAX_TOKENS: int = 800
    SEMANTIC_CHUNKER_MIN_TOKENS: int = 80

    # --- Retry ---
    DECOMPILE_RETRY_LOCK_TTL: int = 10800
    DECOMPILE_RETRY_FAST_DELAY: int = 30
    DECOMPILE_RETRY_STALL_DELAY: int = 60

    # --- Scene ---
    ALLOW_SCENE_PLANNER_FALLBACK: bool = True
    SCENE_PLANNER_TIMEOUT_SECONDS: float = 180.0
    SCENE_PLANNER_TIMEOUT_HARD_CAP_SECONDS: float = 240.0

    # --- Feature flags ---
    CONTEXT_PACK_V2_ENABLED: bool = False
    RAG_QUERY_REWRITE_ENABLED: bool = False
    TARGETED_REVISION_ENABLED: bool = True
    BVSR_ENABLED: bool = False
    BVSR_N: int = 3


settings = Settings()

"""v1.4 — LLM tiering columns and seed prompts

Revision ID: a1001400
Revises: a1001300
Create Date: 2026-04-24

Changes:
- llm_endpoints: add tier VARCHAR(20) NOT NULL DEFAULT 'standard' + CHECK
- prompt_assets: add model_tier VARCHAR(20) NULL + CHECK
- indexes: ix_llm_endpoints_tier, ix_prompt_assets_model_tier
- data backfill: mark embedding endpoints by name/model keywords
- seed 7 new PromptAsset rows for new task_types (ON CONFLICT DO NOTHING semantics)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a1001400"
down_revision = "a1001300"
branch_labels = None
depends_on = None


def upgrade():
    # --- Columns
    op.add_column(
        "llm_endpoints",
        sa.Column("tier", sa.String(20), nullable=False, server_default="standard"),
    )
    op.add_column(
        "prompt_assets",
        sa.Column("model_tier", sa.String(20), nullable=True),
    )

    # --- CHECK constraints
    op.create_check_constraint(
        "ck_llm_endpoints_tier",
        "llm_endpoints",
        "tier IN ('flagship','standard','small','distill','embedding')",
    )
    op.create_check_constraint(
        "ck_prompt_assets_model_tier",
        "prompt_assets",
        "model_tier IS NULL OR model_tier IN ('flagship','standard','small','distill','embedding')",
    )

    # --- Indexes
    op.create_index("ix_llm_endpoints_tier", "llm_endpoints", ["tier"])
    op.create_index("ix_prompt_assets_model_tier", "prompt_assets", ["model_tier"])

    # --- Data backfill: classify embedding endpoints by keywords
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE llm_endpoints
            SET tier = 'embedding'
            WHERE LOWER(COALESCE(name,'')) LIKE '%embed%'
               OR LOWER(COALESCE(default_model,'')) LIKE ANY(ARRAY['%embed%','%bge%','%e5%']);
            """
        )
    )

    # --- Seed 7 built-in prompts if missing (idempotent)
    def _ensure_prompt(task_type: str, name: str, category: str, model_tier: str | None):
        exists = bind.execute(
            sa.text(
                "SELECT 1 FROM prompt_assets WHERE task_type=:t AND is_active=1 LIMIT 1"
            ),
            {"t": task_type},
        ).fetchone()
        if exists:
            return
        bind.execute(
            sa.text(
                """
                INSERT INTO prompt_assets (
                    id, task_type, name, description, mode, system_prompt,
                    user_template, output_schema, context_policy, version, is_active,
                    success_count, fail_count, avg_score,
                    endpoint_id, model_name, temperature, max_tokens,
                    category, "order", always_enabled, name_en, description_en, model_tier,
                    created_at, updated_at
                ) VALUES (
                    gen_random_uuid(), :task_type, :name, :desc, 'text', :sys,
                    '', NULL, 'default', 1, 1,
                    0, 0, 0,
                    NULL, '', 0.7, 4096,
                    :category, 0, 0, :name_en, :desc_en, :model_tier,
                    NOW(), NOW()
                )
                """
            ),
            {
                "task_type": task_type,
                "name": name,
                "desc": f"Built-in {task_type} seed for v1.4",
                "sys": "",
                "category": category,
                "name_en": name,
                "desc_en": f"Built-in {task_type} seed for v1.4",
                "model_tier": model_tier,
            },
        )

    seeds = [
        ("critic_hard", "硬伤检查", "Evaluation", "standard"),
        ("critic_soft", "软指标检查", "Evaluation", "small"),
        ("consistency_llm_check", "一致性判官", "Evaluation", "standard"),
        ("rag_query_rewrite", "RAG 检索改写", "RAG", "small"),
        ("characters_extraction", "人物抽取", "Extraction", "standard"),
        ("world_rules_extraction", "世界设定抽取", "Extraction", "standard"),
        ("relationships_extraction", "关系抽取", "Extraction", "small"),
    ]
    for t, n, c, mt in seeds:
        _ensure_prompt(t, n, c, mt)


def downgrade():
    bind = op.get_bind()
    # Remove seeded prompts (only those created by this migration — match task_type and is_active)
    for t in [
        "critic_hard",
        "critic_soft",
        "consistency_llm_check",
        "rag_query_rewrite",
        "characters_extraction",
        "world_rules_extraction",
        "relationships_extraction",
    ]:
        bind.execute(
            sa.text(
                "DELETE FROM prompt_assets WHERE task_type=:t AND is_active=1"
            ),
            {"t": t},
        )

    # Drop indexes and constraints
    op.drop_index("ix_prompt_assets_model_tier")
    op.drop_index("ix_llm_endpoints_tier")
    op.drop_constraint("ck_prompt_assets_model_tier", "prompt_assets", type_="check")
    op.drop_constraint("ck_llm_endpoints_tier", "llm_endpoints", type_="check")

    # Drop columns
    op.drop_column("prompt_assets", "model_tier")
    op.drop_column("llm_endpoints", "tier")

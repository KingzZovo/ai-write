"""v0.8: writing engine assets (rules / beat patterns / anti-ai traps / genre profiles / tool specs).

Revision ID: a0800000
Revises: a0700000
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a0800000"
down_revision = "a0700000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # writing_rules ---------------------------------------------------------
    op.create_table(
        "writing_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("genre", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("category", sa.String(length=32), nullable=False),  # pacing|dialogue|hook|description
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("rule_text", sa.Text(), nullable=False),
        sa.Column("examples_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_writing_rules_genre", "writing_rules", ["genre"])
    op.create_index("ix_writing_rules_category", "writing_rules", ["category"])
    op.create_index("ix_writing_rules_is_active", "writing_rules", ["is_active"])

    # beat_patterns ---------------------------------------------------------
    op.create_table(
        "beat_patterns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("genre", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("stage", sa.String(length=32), nullable=False),  # opening|volume_end|climax|turning|closure|...
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("trigger_conditions_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("reusable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_beat_patterns_genre", "beat_patterns", ["genre"])
    op.create_index("ix_beat_patterns_stage", "beat_patterns", ["stage"])

    # anti_ai_traps ---------------------------------------------------------
    op.create_table(
        "anti_ai_traps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("locale", sa.String(length=16), nullable=False, server_default="zh-CN"),
        sa.Column("pattern_type", sa.String(length=16), nullable=False),  # keyword|regex|ngram
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False, server_default="soft"),  # hard|soft
        sa.Column("replacement_hint", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_anti_ai_traps_pattern_type", "anti_ai_traps", ["pattern_type"])
    op.create_index("ix_anti_ai_traps_severity", "anti_ai_traps", ["severity"])
    op.create_index("ix_anti_ai_traps_is_active", "anti_ai_traps", ["is_active"])

    # genre_profiles --------------------------------------------------------
    op.create_table(
        "genre_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(length=64), nullable=False, unique=True),  # xianxia|urban|scifi|...
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("default_beat_pattern_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("default_writing_rule_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_genre_profiles_code", "genre_profiles", ["code"])

    # tool_specs ------------------------------------------------------------
    op.create_table(
        "tool_specs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("input_schema_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("output_schema_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("handler", sa.String(length=32), nullable=False),  # python_callable|sql|qdrant|llm
        sa.Column("config_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tool_specs_name", "tool_specs", ["name"])
    op.create_index("ix_tool_specs_is_active", "tool_specs", ["is_active"])

    # project genre binding ------------------------------------------------
    # Optional soft pointer to genre_profiles.code on projects.
    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column(
                "genre_profile_code",
                sa.String(length=64),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("genre_profile_code")

    op.drop_index("ix_tool_specs_is_active", table_name="tool_specs")
    op.drop_index("ix_tool_specs_name", table_name="tool_specs")
    op.drop_table("tool_specs")

    op.drop_index("ix_genre_profiles_code", table_name="genre_profiles")
    op.drop_table("genre_profiles")

    op.drop_index("ix_anti_ai_traps_is_active", table_name="anti_ai_traps")
    op.drop_index("ix_anti_ai_traps_severity", table_name="anti_ai_traps")
    op.drop_index("ix_anti_ai_traps_pattern_type", table_name="anti_ai_traps")
    op.drop_table("anti_ai_traps")

    op.drop_index("ix_beat_patterns_stage", table_name="beat_patterns")
    op.drop_index("ix_beat_patterns_genre", table_name="beat_patterns")
    op.drop_table("beat_patterns")

    op.drop_index("ix_writing_rules_is_active", table_name="writing_rules")
    op.drop_index("ix_writing_rules_category", table_name="writing_rules")
    op.drop_index("ix_writing_rules_genre", table_name="writing_rules")
    op.drop_table("writing_rules")

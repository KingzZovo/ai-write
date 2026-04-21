"""v0.8: writing engine assets (rules / beat patterns / anti-ai traps / genre profiles / tool specs).

Revision ID: a0800000
Revises: a0700000
Create Date: 2026-04-22

The main FastAPI lifespan runs ``Base.metadata.create_all`` as a safety net
before alembic is invoked, which means these tables may already exist when
alembic runs on an environment that booted at least once with the v0.8 models
imported. We therefore make this migration idempotent by inspecting the
live schema before each DDL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "a0800000"
down_revision = "a0700000"
branch_labels = None
depends_on = None


def _inspector():
    return inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in set(_inspector().get_table_names())


def _has_index(table: str, name: str) -> bool:
    try:
        return any(ix["name"] == name for ix in _inspector().get_indexes(table))
    except Exception:
        return False


def _has_column(table: str, column: str) -> bool:
    try:
        return any(c["name"] == column for c in _inspector().get_columns(table))
    except Exception:
        return False


def _safe_create_index(name: str, table: str, cols: list[str]) -> None:
    if _has_table(table) and not _has_index(table, name):
        op.create_index(name, table, cols)


def upgrade() -> None:
    # writing_rules ---------------------------------------------------------
    if not _has_table("writing_rules"):
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
    _safe_create_index("ix_writing_rules_genre", "writing_rules", ["genre"])
    _safe_create_index("ix_writing_rules_category", "writing_rules", ["category"])
    _safe_create_index("ix_writing_rules_is_active", "writing_rules", ["is_active"])

    # beat_patterns ---------------------------------------------------------
    if not _has_table("beat_patterns"):
        op.create_table(
            "beat_patterns",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("genre", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("stage", sa.String(length=32), nullable=False),  # opening|turning|climax|volume_end|closure
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("trigger_conditions_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("reusable", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    _safe_create_index("ix_beat_patterns_genre", "beat_patterns", ["genre"])
    _safe_create_index("ix_beat_patterns_stage", "beat_patterns", ["stage"])
    _safe_create_index("ix_beat_patterns_is_active", "beat_patterns", ["is_active"])

    # anti_ai_traps ---------------------------------------------------------
    if not _has_table("anti_ai_traps"):
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
    _safe_create_index("ix_anti_ai_traps_locale", "anti_ai_traps", ["locale"])
    _safe_create_index("ix_anti_ai_traps_severity", "anti_ai_traps", ["severity"])
    _safe_create_index("ix_anti_ai_traps_is_active", "anti_ai_traps", ["is_active"])

    # genre_profiles --------------------------------------------------------
    if not _has_table("genre_profiles"):
        op.create_table(
            "genre_profiles",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("code", sa.String(length=64), nullable=False, unique=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("default_beat_pattern_ids", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("default_writing_rule_ids", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
    _safe_create_index("ix_genre_profiles_is_active", "genre_profiles", ["is_active"])

    # tool_specs ------------------------------------------------------------
    if not _has_table("tool_specs"):
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
    _safe_create_index("ix_tool_specs_is_active", "tool_specs", ["is_active"])

    # projects.genre_profile_code ------------------------------------------
    if _has_table("projects") and not _has_column("projects", "genre_profile_code"):
        with op.batch_alter_table("projects") as batch:
            batch.add_column(sa.Column("genre_profile_code", sa.String(length=64), nullable=True))


def downgrade() -> None:
    if _has_table("projects") and _has_column("projects", "genre_profile_code"):
        with op.batch_alter_table("projects") as batch:
            batch.drop_column("genre_profile_code")
    for table in ("tool_specs", "genre_profiles", "anti_ai_traps", "beat_patterns", "writing_rules"):
        if _has_table(table):
            op.drop_table(table)

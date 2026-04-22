"""v0.9: settings-book upgrade — relationship evolution + settings change log.

Revision ID: a0900000
Revises: a0800000
Create Date: 2026-04-22

Adds:
- relationships.since_volume_id (FK volumes.id nullable)
- relationships.until_volume_id (FK volumes.id nullable)
- relationships.evolution_json (JSON default '[]')
- new table settings_change_log (audit trail for characters/world_rules/relationships edits)

Idempotent — same inspector-gated pattern as a0800000 so alembic upgrade is
safe even when FastAPI lifespan's ``Base.metadata.create_all`` safety net has
already created the new table after the backend booted with v0.9 ORM models.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "a0900000"
down_revision = "a0800000"
branch_labels = None
depends_on = None


def _inspector():
    return inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in set(_inspector().get_table_names())


def _has_column(table: str, column: str) -> bool:
    try:
        return any(c["name"] == column for c in _inspector().get_columns(table))
    except Exception:
        return False


def _has_index(table: str, name: str) -> bool:
    try:
        return any(ix["name"] == name for ix in _inspector().get_indexes(table))
    except Exception:
        return False


def _has_fk(table: str, name: str) -> bool:
    try:
        return any(fk.get("name") == name for fk in _inspector().get_foreign_keys(table))
    except Exception:
        return False


def _safe_create_index(name: str, table: str, cols: list[str]) -> None:
    if _has_table(table) and not _has_index(table, name):
        op.create_index(name, table, cols)


def upgrade() -> None:
    # relationships: evolution columns --------------------------------------
    if _has_table("relationships"):
        if not _has_column("relationships", "since_volume_id"):
            op.add_column(
                "relationships",
                sa.Column("since_volume_id", postgresql.UUID(as_uuid=True), nullable=True),
            )
            if not _has_fk("relationships", "fk_relationships_since_volume"):
                op.create_foreign_key(
                    "fk_relationships_since_volume",
                    "relationships",
                    "volumes",
                    ["since_volume_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
        if not _has_column("relationships", "until_volume_id"):
            op.add_column(
                "relationships",
                sa.Column("until_volume_id", postgresql.UUID(as_uuid=True), nullable=True),
            )
            if not _has_fk("relationships", "fk_relationships_until_volume"):
                op.create_foreign_key(
                    "fk_relationships_until_volume",
                    "relationships",
                    "volumes",
                    ["until_volume_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
        if not _has_column("relationships", "evolution_json"):
            op.add_column(
                "relationships",
                sa.Column(
                    "evolution_json",
                    sa.JSON(),
                    nullable=False,
                    server_default="[]",
                ),
            )
    _safe_create_index("ix_relationships_since_volume", "relationships", ["since_volume_id"])
    _safe_create_index("ix_relationships_until_volume", "relationships", ["until_volume_id"])

    # settings_change_log ---------------------------------------------------
    if not _has_table("settings_change_log"):
        op.create_table(
            "settings_change_log",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "project_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("actor_type", sa.String(length=16), nullable=False, server_default="user"),  # user|agent|critic|system
            sa.Column("actor_id", sa.String(length=128), nullable=True),  # user email / agent id etc.
            sa.Column("target_type", sa.String(length=32), nullable=False),  # character|world_rule|relationship
            sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("action", sa.String(length=16), nullable=False, server_default="update"),  # create|update|delete
            sa.Column("before_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("after_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
    _safe_create_index("ix_settings_change_log_project", "settings_change_log", ["project_id"])
    _safe_create_index("ix_settings_change_log_target_type", "settings_change_log", ["target_type"])
    _safe_create_index("ix_settings_change_log_created_at", "settings_change_log", ["created_at"])


def downgrade() -> None:
    # non-destructive; keep data by default. Explicit downgrade removes the
    # new columns + table if the operator really wants it.
    if _has_table("settings_change_log"):
        op.drop_table("settings_change_log")
    if _has_table("relationships"):
        for col in ("evolution_json", "until_volume_id", "since_volume_id"):
            if _has_column("relationships", col):
                op.drop_column("relationships", col)

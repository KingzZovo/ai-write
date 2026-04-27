"""v1.5.0 C4 — cascade_tasks table for cross-chapter cascade auto-regenerate.

Revision ID: a1001800
Revises: a1001700
Create Date: 2026-04-27

When a chapter evaluation reports `rounds_exhausted=true` AND `overall <
threshold` AND `issues_json` carries severity>=high cross_chapter / foreshadow
/ consistency issues, the C4 planner derives the affected upstream entities
(chapter / outline / character / world_rule) and enqueues one row in this
table per (entity_type, entity_id, severity) tuple. UNIQUE on
(source_chapter_id, target_entity_type, target_entity_id, severity) provides
the idempotency key; (project_id, status) is the per-project FIFO index.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a1001800"
down_revision = "a1001700"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cascade_tasks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_chapter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chapters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_evaluation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chapter_evaluations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_entity_type", sa.String(20), nullable=False),
        sa.Column(
            "target_entity_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("issue_summary", sa.Text, nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "parent_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cascade_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "target_entity_type IN ('chapter','outline','character','world_rule')",
            name="ck_cascade_tasks_target_entity_type",
        ),
        sa.CheckConstraint(
            "severity IN ('high','critical')",
            name="ck_cascade_tasks_severity",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','done','failed','skipped')",
            name="ck_cascade_tasks_status",
        ),
        sa.UniqueConstraint(
            "source_chapter_id",
            "target_entity_type",
            "target_entity_id",
            "severity",
            name="uq_cascade_tasks_idem",
        ),
    )
    op.create_index(
        "ix_cascade_tasks_project_status",
        "cascade_tasks",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_cascade_tasks_source_chapter",
        "cascade_tasks",
        ["source_chapter_id"],
    )
    op.create_index(
        "ix_cascade_tasks_target_entity",
        "cascade_tasks",
        ["target_entity_type", "target_entity_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_cascade_tasks_target_entity", table_name="cascade_tasks")
    op.drop_index("ix_cascade_tasks_source_chapter", table_name="cascade_tasks")
    op.drop_index("ix_cascade_tasks_project_status", table_name="cascade_tasks")
    op.drop_table("cascade_tasks")

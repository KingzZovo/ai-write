"""v1.5.0 C2 Step D — evaluate_tasks table for async eval API.

Revision ID: a1001700
Revises: a1001601
Create Date: 2026-04-27

Provides backing storage for the new POST /api/evaluate/start +
GET /api/evaluate/tasks/{task_id} flow that decouples the 30-90s
evaluator LLM call from the request thread (currently sync /evaluate is
fine within nginx 300s but blocks UI; this lets the UI poll).

Schema:
  evaluate_tasks (
    id            UUID PK,
    chapter_id    UUID FK -> chapters.id ON DELETE CASCADE,
    status        TEXT     -- 'pending' | 'running' | 'completed' | 'failed'
    round_idx     INTEGER  -- 0 = initial, 1+ = post auto-revise rounds
    caller        TEXT     -- which code path enqueued (audit / debug)
    result_json   JSONB    -- EvaluationResult.to_dict() snapshot
    error_text    TEXT     -- last failure message (if failed)
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
  )
  + indexes on (chapter_id, status).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a1001700"
down_revision = "a1001601"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluate_tasks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "chapter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chapters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("round_idx", sa.Integer, nullable=False, server_default="0"),
        sa.Column("caller", sa.String(100), nullable=False, server_default=""),
        sa.Column("result_json", postgresql.JSONB, nullable=True),
        sa.Column("error_text", sa.Text, nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_evaluate_tasks_chapter_id", "evaluate_tasks", ["chapter_id"]
    )
    op.create_index("ix_evaluate_tasks_status", "evaluate_tasks", ["status"])


def downgrade() -> None:
    op.drop_index("ix_evaluate_tasks_status", table_name="evaluate_tasks")
    op.drop_index("ix_evaluate_tasks_chapter_id", table_name="evaluate_tasks")
    op.drop_table("evaluate_tasks")

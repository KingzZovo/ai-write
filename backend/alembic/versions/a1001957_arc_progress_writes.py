"""Keep author arc progress tied to saved manuscripts and fixed requests.

Revision ID: a1001957
Revises: a1001956
"""
from alembic import op
import sqlalchemy as sa

revision = "a1001957"
down_revision = "a1001956"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("arc_progress_writes",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("volume_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("chapter_id", sa.UUID()),
        sa.Column("manuscript_hash", sa.String(64)),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("before_json", sa.JSON(), nullable=False),
        sa.Column("manuscript_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("sequence > 0", name="ck_arc_progress_sequence_positive"),
        sa.UniqueConstraint("project_id", "volume_id", "sequence", name="uq_arc_progress_sequence"))
    op.create_index("ix_arc_progress_history", "arc_progress_writes", ["project_id", "volume_id", "sequence"])


def downgrade():
    rows = sa.table("arc_progress_writes", sa.column("id", sa.UUID()))
    if op.get_bind().execute(sa.select(rows.c.id).limit(1)).first() is not None:
        raise RuntimeError("已有作者弧进度和保存稿确认历史，不能直接删除回执。")
    op.drop_table("arc_progress_writes")

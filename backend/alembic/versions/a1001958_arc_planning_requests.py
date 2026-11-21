"""Persist fixed arc planning requests, phase attempts and atomic delivery.

Revision ID: a1001958
Revises: a1001957
"""
from alembic import op
import sqlalchemy as sa

revision = "a1001958"
down_revision = "a1001957"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("arc_planning_requests",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("source_json", sa.JSON(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("journal_json", sa.JSON(), nullable=False),
        sa.Column("journal_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("phase", sa.String(40), nullable=False, server_default="meta"),
        sa.Column("target_chapters", sa.Integer(), nullable=False),
        sa.Column("target_volume_idx", sa.Integer(), nullable=False),
        sa.Column("accepted_chapters", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("execution_id", sa.UUID()),
        sa.Column("execution_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("execution_history_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=False, server_default=""),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("result_json", sa.JSON(none_as_null=True)),
        sa.Column("result_hash", sa.String(64)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("target_chapters BETWEEN 4 AND 40", name="ck_arc_planning_target"),
        sa.CheckConstraint("target_volume_idx > 0", name="ck_arc_planning_volume_index"),
        sa.CheckConstraint("execution_attempts BETWEEN 0 AND 3", name="ck_arc_planning_execution_budget"),
        sa.CheckConstraint("revision > 0", name="ck_arc_planning_revision"),
        sa.CheckConstraint("accepted_chapters BETWEEN 0 AND target_chapters", name="ck_arc_planning_progress"),
        sa.CheckConstraint("status IN ('pending', 'running', 'needs_review', 'completed', 'cancelled')", name="ck_arc_planning_status"))
    op.create_index("ix_arc_planning_history", "arc_planning_requests", ["project_id", "created_at", "id"])
    op.create_index("ix_arc_planning_dispatch", "arc_planning_requests", ["status", "dispatched_at", "heartbeat_at"])
    op.create_index("uq_arc_planning_active", "arc_planning_requests", ["project_id"], unique=True,
                    postgresql_where=sa.text("status IN ('pending', 'running')"),
                    sqlite_where=sa.text("status IN ('pending', 'running')"))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # An acceptance that commits after an unlocked empty check must not
        # be discarded by DROP. Wait, then check under the same DDL lock.
        bind.execute(sa.text("LOCK TABLE arc_planning_requests IN ACCESS EXCLUSIVE MODE"))
    rows = sa.table("arc_planning_requests", sa.column("id", sa.UUID()))
    if bind.execute(sa.select(rows.c.id).limit(1)).first() is not None:
        raise RuntimeError("已有弧规划请求、尝试或创建回执，不能直接删除原记录。")
    op.drop_table("arc_planning_requests")

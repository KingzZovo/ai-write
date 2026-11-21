"""v0.5 — Prompt-centric routing, call logs, ask-user pauses

Revision ID: a0504000
Revises: b7ff198ef96c
Create Date: 2026-04-21

Changes:
- prompt_assets: add endpoint_id, model_name, temperature, max_tokens,
  category, order, always_enabled, name_en, description_en
- Create llm_call_logs, ask_user_pauses
- Migrate model_configs rows into matching prompt_assets
- Drop model_configs table
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import column, select, table


revision = "a0504000"
down_revision = "b7ff198ef96c"
branch_labels = None
depends_on = None


def upgrade():
    # --- prompt_assets new columns
    op.add_column(
        "prompt_assets",
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("prompt_assets", sa.Column("model_name", sa.String(200), server_default=""))
    op.add_column("prompt_assets", sa.Column("temperature", sa.Float, server_default="0.7"))
    op.add_column("prompt_assets", sa.Column("max_tokens", sa.Integer, server_default="4096"))
    op.add_column("prompt_assets", sa.Column("category", sa.String(50), server_default="Core"))
    op.add_column("prompt_assets", sa.Column("order", sa.Integer, server_default="0"))
    op.add_column("prompt_assets", sa.Column("always_enabled", sa.Integer, server_default="0"))
    op.add_column("prompt_assets", sa.Column("name_en", sa.String(200), server_default=""))
    op.add_column("prompt_assets", sa.Column("description_en", sa.Text, server_default=""))
    op.create_foreign_key(
        "fk_prompt_assets_endpoint",
        "prompt_assets",
        "llm_endpoints",
        ["endpoint_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- llm_call_logs
    op.create_table(
        "llm_call_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "prompt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prompt_assets.id", ondelete="SET NULL"),
        ),
        sa.Column("task_type", sa.String(50), nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "chapter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chapters.id", ondelete="SET NULL"),
        ),
        sa.Column("messages_json", postgresql.JSON, nullable=False),
        sa.Column("rag_hits_json", postgresql.JSON, nullable=True),
        sa.Column("response_text", sa.Text, server_default=""),
        sa.Column("input_tokens", sa.Integer, server_default="0"),
        sa.Column("output_tokens", sa.Integer, server_default="0"),
        sa.Column("latency_ms", sa.Integer, server_default="0"),
        sa.Column("model", sa.String(200), server_default=""),
        sa.Column(
            "endpoint_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("llm_endpoints.id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.String(20), server_default="ok"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")
        ),
    )
    op.create_index(
        "ix_llm_call_logs_project_created",
        "llm_call_logs",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_llm_call_logs_chapter_created",
        "llm_call_logs",
        ["chapter_id", "created_at"],
    )
    op.create_index("ix_llm_call_logs_task_type", "llm_call_logs", ["task_type"])

    # --- ask_user_pauses
    op.create_table(
        "ask_user_pauses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "chapter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chapters.id", ondelete="CASCADE"),
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")
        ),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_ask_user_project_status",
        "ask_user_pauses",
        ["project_id", "status", "created_at"],
    )
    op.create_index("ix_ask_user_run", "ask_user_pauses", ["run_id"])

    # --- Data migration: copy model_configs into prompt_assets
    bind = op.get_bind()
    model_configs = table(
        "model_configs",
        column("task_type", sa.String),
        column("endpoint_id", postgresql.UUID(as_uuid=True)),
        column("model_name", sa.String),
        column("temperature", sa.Float),
        column("max_tokens", sa.Integer),
    )
    rows = bind.execute(
        select(
            model_configs.c.task_type,
            model_configs.c.endpoint_id,
            model_configs.c.model_name,
            model_configs.c.temperature,
            model_configs.c.max_tokens,
        )
    ).fetchall()
    for row in rows:
        bind.execute(
            sa.text(
                "UPDATE prompt_assets "
                "SET endpoint_id = :eid, model_name = :m, "
                "    temperature = :t, max_tokens = :tok "
                "WHERE task_type = :task AND is_active = 1"
            ),
            {
                "eid": row.endpoint_id,
                "m": row.model_name or "",
                "t": row.temperature,
                "tok": row.max_tokens,
                "task": row.task_type,
            },
        )

    # --- Drop model_configs
    op.drop_table("model_configs")


def downgrade():
    # Recreate model_configs (empty) so a downgrade at least restores shape
    op.create_table(
        "model_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_type", sa.String(50), unique=True, nullable=False),
        sa.Column(
            "endpoint_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("llm_endpoints.id", ondelete="SET NULL"),
        ),
        sa.Column("model_name", sa.String(200), server_default=""),
        sa.Column("temperature", sa.Float, server_default="0.7"),
        sa.Column("max_tokens", sa.Integer, server_default="4096"),
        sa.Column("params_json", postgresql.JSON, server_default="{}"),
    )
    op.drop_index("ix_ask_user_run")
    op.drop_index("ix_ask_user_project_status")
    op.drop_table("ask_user_pauses")
    op.drop_index("ix_llm_call_logs_task_type")
    op.drop_index("ix_llm_call_logs_chapter_created")
    op.drop_index("ix_llm_call_logs_project_created")
    op.drop_table("llm_call_logs")
    op.drop_constraint("fk_prompt_assets_endpoint", "prompt_assets", type_="foreignkey")
    for col in [
        "description_en",
        "name_en",
        "always_enabled",
        "order",
        "category",
        "max_tokens",
        "temperature",
        "model_name",
        "endpoint_id",
    ]:
        op.drop_column("prompt_assets", col)

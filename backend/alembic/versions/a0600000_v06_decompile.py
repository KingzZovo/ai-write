"""v0.6 — reference-book decompile: slices + style/beat cards

Revision ID: a0600000
Revises: a0504000
Create Date: 2026-04-21

Changes:
- Create reference_book_slices
- Create style_profile_cards
- Create beat_sheet_cards
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "a0600000"
down_revision = "a0504000"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reference_book_slices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("book_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slice_type", sa.String(30), nullable=False, server_default="scene"),
        sa.Column("chapter_idx", sa.Integer, nullable=True),
        sa.Column("sequence_id", sa.Integer, nullable=False, server_default="0"),
        sa.Column("start_offset", sa.Integer, nullable=False, server_default="0"),
        sa.Column("end_offset", sa.Integer, nullable=False, server_default="0"),
        sa.Column("raw_text", sa.Text, nullable=False),
        sa.Column("token_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("meta_json", postgresql.JSON, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["book_id"], ["reference_books.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_reference_book_slices_book_id", "reference_book_slices", ["book_id"])
    op.create_index("ix_reference_book_slices_book_seq", "reference_book_slices", ["book_id", "sequence_id"])

    op.create_table(
        "style_profile_cards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("book_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_json", postgresql.JSON, nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("qdrant_point_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["book_id"], ["reference_books.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["slice_id"], ["reference_book_slices.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_style_profile_cards_book_id", "style_profile_cards", ["book_id"])
    op.create_index("ix_style_profile_cards_slice_id", "style_profile_cards", ["slice_id"])

    op.create_table(
        "beat_sheet_cards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("book_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("beat_json", postgresql.JSON, nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("qdrant_point_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["book_id"], ["reference_books.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["slice_id"], ["reference_book_slices.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_beat_sheet_cards_book_id", "beat_sheet_cards", ["book_id"])
    op.create_index("ix_beat_sheet_cards_slice_id", "beat_sheet_cards", ["slice_id"])


def downgrade():
    op.drop_index("ix_beat_sheet_cards_slice_id", "beat_sheet_cards")
    op.drop_index("ix_beat_sheet_cards_book_id", "beat_sheet_cards")
    op.drop_table("beat_sheet_cards")

    op.drop_index("ix_style_profile_cards_slice_id", "style_profile_cards")
    op.drop_index("ix_style_profile_cards_book_id", "style_profile_cards")
    op.drop_table("style_profile_cards")

    op.drop_index("ix_reference_book_slices_book_seq", "reference_book_slices")
    op.drop_index("ix_reference_book_slices_book_id", "reference_book_slices")
    op.drop_table("reference_book_slices")

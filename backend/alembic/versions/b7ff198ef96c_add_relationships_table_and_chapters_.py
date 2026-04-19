"""add relationships table and chapters.target_words

Revision ID: b7ff198ef96c
Revises: 27738d6e6203
Create Date: 2026-04-19 16:21:22.279471

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b7ff198ef96c'
down_revision: Union[str, Sequence[str], None] = '27738d6e6203'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "relationships",
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
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rel_type", sa.String(50), nullable=False),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("note", sa.Text, nullable=False, server_default=""),
        sa.Column("sentiment", sa.String(20), nullable=False, server_default="neutral"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_relationships_project_id", "relationships", ["project_id"])
    op.create_index("ix_relationships_source_id", "relationships", ["source_id"])
    op.create_index("ix_relationships_target_id", "relationships", ["target_id"])

    op.add_column("chapters", sa.Column("target_words", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("chapters", "target_words")
    op.drop_index("ix_relationships_target_id", table_name="relationships")
    op.drop_index("ix_relationships_source_id", table_name="relationships")
    op.drop_index("ix_relationships_project_id", table_name="relationships")
    op.drop_table("relationships")

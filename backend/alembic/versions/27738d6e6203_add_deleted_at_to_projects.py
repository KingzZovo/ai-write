"""add deleted_at to projects

Revision ID: 27738d6e6203
Revises: 482b5e188065
Create Date: 2026-04-19 09:11:31.365073

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '27738d6e6203'
down_revision: Union[str, Sequence[str], None] = '482b5e188065'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "projects",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_projects_deleted_at_null",
        "projects",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_projects_deleted_at_null", table_name="projects")
    op.drop_column("projects", "deleted_at")

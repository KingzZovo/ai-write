"""Immutable author volume-outline receipts, retained after volume deletion."""
from sqlalchemy import Column, DateTime, ForeignKey, Index, JSON, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base


class VolumeOutlineWrite(Base):
    __tablename__ = 'volume_outline_writes'

    id = Column(UUID(as_uuid=True), primary_key=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey('projects.id', ondelete='CASCADE'), nullable=False)
    volume_id = Column(UUID(as_uuid=True), nullable=False)
    request_hash = Column(String(64), nullable=False)
    before_json = Column(JSON, nullable=False)
    result_json = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (Index('ix_volume_outline_write_history', 'project_id', 'volume_id', 'created_at', 'id'),)

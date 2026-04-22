"""v0.9: settings change log — audit trail for characters / world_rules / relationships edits."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SettingsChangeLog(Base):
    """Audit log for settings-book mutations.

    Every write to characters / world_rules / relationships via the authoring
    API goes through ``services.change_log.record_change`` which emits one row
    here. The frontend /projects/{id}/changelog timeline page consumes this.
    """

    __tablename__ = "settings_change_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_type = Column(String(16), nullable=False, default="user")  # user|agent|critic|system
    actor_id = Column(String(128), nullable=True)
    target_type = Column(String(32), nullable=False)  # character|world_rule|relationship
    target_id = Column(UUID(as_uuid=True), nullable=True)
    action = Column(String(16), nullable=False, default="update")  # create|update|delete
    before_json = Column(JSON, default=dict)
    after_json = Column(JSON, default=dict)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

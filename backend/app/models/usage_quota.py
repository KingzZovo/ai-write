"""UsageQuota -- per-user monthly token/cost usage + hard quota.

One row per (user_id, month_ym). user_id is the JWT subject (username string),
not a UUID -- there is no local ``users`` table.
"""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, DateTime, String, UniqueConstraint

from app.db.session import Base

_utcnow = lambda: datetime.now(timezone.utc)  # noqa: E731


class UsageQuota(Base):
    __tablename__ = "usage_quotas"
    __table_args__ = (
        UniqueConstraint("user_id", "month_ym", name="uq_usage_quotas_user_month"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, index=True)
    month_ym = Column(String(7), nullable=False, index=True)
    prompt_tokens = Column(BigInteger, nullable=False, default=0)
    completion_tokens = Column(BigInteger, nullable=False, default=0)
    cost_cents = Column(BigInteger, nullable=False, default=0)
    quota_cents = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

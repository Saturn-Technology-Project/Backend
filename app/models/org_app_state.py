from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import utcnow
from app.db.database import Base


class OrgAppState(Base):
    """Team agents that Saturn App syncs for everyone in the organization."""

    __tablename__ = "org_app_state"

    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    agents: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

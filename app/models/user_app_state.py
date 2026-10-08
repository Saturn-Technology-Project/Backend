from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import utcnow
from app.db.database import Base


class UserAppState(Base):
    """Agents and config that Saturn App syncs between its own installs."""

    __tablename__ = "user_app_state"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    agents: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    messages: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

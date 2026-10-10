from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import utcnow
from app.db.database import Base


class UserAppState(Base):
    """Agents, conversations, tasks, wakes, and notes that Saturn syncs for one user."""

    __tablename__ = "user_app_state"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    agents: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    messages: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    tasks: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    wakes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    memories: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

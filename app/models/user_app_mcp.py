from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import utcnow
from app.db.database import Base


class UserAppMcp(Base):
    """MCP the user connected inside Saturn App. Separate from Saturn Cloud connections."""

    __tablename__ = "user_app_mcp"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    servers: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    attachments: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

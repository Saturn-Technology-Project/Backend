from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    invited_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="Operator", nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="operator", nullable=False)
    permissions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    agent_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    mcp_server_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    organization = relationship("Organization", back_populates="invitations")
    invited_by = relationship("User", foreign_keys=[invited_by_user_id])

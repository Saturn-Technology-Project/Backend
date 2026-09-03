from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base


class Connector(Base):
    __tablename__ = "connectors"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_connectors_organization_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), default="Saturn Connector", nullable=False)
    environment_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    version: Mapped[str] = mapped_column(String(32), default="0.1.0", nullable=False)
    capabilities: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    token_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    enrollment_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    organization = relationship("Organization", back_populates="connectors")
    agents = relationship("Agent", back_populates="connector")
    mcp_servers = relationship("McpServer", back_populates="connector")

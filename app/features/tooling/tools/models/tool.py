from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base


class Tool(Base):
    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_tools_org_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    type: Mapped[str] = mapped_column(String(32), default="mcp", nullable=False)
    mcp_endpoint: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    permissions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    upstream_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    mcp_server_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("mcp_servers.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    organization = relationship("Organization", back_populates="tools")
    mcp_server = relationship("McpServer", back_populates="tools")

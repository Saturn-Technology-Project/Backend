from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base
from app.models.associations import agent_mcp_servers, employee_mcp_servers


class McpServer(Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_mcp_servers_org_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    auth_ciphertext: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    connector_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("connectors.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    organization = relationship("Organization", back_populates="mcp_servers")
    connector = relationship("Connector", back_populates="mcp_servers")
    tools = relationship("Tool", back_populates="mcp_server")
    employees = relationship(
        "Employee", secondary=employee_mcp_servers, back_populates="mcp_servers"
    )
    agents = relationship("Agent", secondary=agent_mcp_servers, back_populates="mcp_servers")

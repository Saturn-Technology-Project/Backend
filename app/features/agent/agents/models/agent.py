from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base
from app.shared.models.associations import agent_mcp_servers, agent_policies, employee_agents


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="claude-sonnet", nullable=False)
    runtime_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    tool_set_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("tool_sets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    organization = relationship("Organization", back_populates="agents")
    runs = relationship("Run", back_populates="agent")
    employees = relationship("Employee", secondary=employee_agents, back_populates="agents")
    mcp_servers = relationship("McpServer", secondary=agent_mcp_servers, back_populates="agents")
    policies = relationship("Policy", secondary=agent_policies, back_populates="agents")

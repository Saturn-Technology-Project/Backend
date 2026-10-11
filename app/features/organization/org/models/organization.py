from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    photo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    plan: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="personal", nullable=False)
    deployment_mode: Mapped[str] = mapped_column(String(32), default="cloud", nullable=False)
    runtime_region: Mapped[str] = mapped_column(String(64), default="asia-east1", nullable=False)
    runtime_version: Mapped[str] = mapped_column(String(32), default="0.1.0", nullable=False)
    billing: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    agents = relationship("Agent", back_populates="organization")
    tools = relationship("Tool", back_populates="organization")
    policies = relationship("Policy", back_populates="organization")
    runs = relationship("Run", back_populates="organization")
    users = relationship("User", back_populates="organization")
    employees = relationship("Employee", back_populates="organization")
    invitations = relationship("Invitation", back_populates="organization")
    mcp_servers = relationship("McpServer", back_populates="organization")
    audit_logs = relationship("AuditLog", back_populates="organization")

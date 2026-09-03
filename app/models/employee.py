from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base
from app.models.associations import employee_agents, employee_mcp_servers


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = (UniqueConstraint("user_id", name="uq_employees_user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), default="Operator", nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="operator", nullable=False)
    permissions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    organization = relationship("Organization", back_populates="employees")
    user = relationship("User", back_populates="employee")
    agents = relationship("Agent", secondary=employee_agents, back_populates="employees")
    mcp_servers = relationship(
        "McpServer", secondary=employee_mcp_servers, back_populates="employees"
    )

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base
from app.shared.models.associations import agent_policies


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    environment: Mapped[str] = mapped_column(String(64), default="production", nullable=False)
    tool_set_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("tool_sets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    definition: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    organization = relationship("Organization", back_populates="policies")
    rules = relationship(
        "PolicyRule",
        back_populates="policy",
        cascade="all, delete-orphan",
        order_by="PolicyRule.position",
    )
    agents = relationship("Agent", secondary=agent_policies, back_populates="policies")


class PolicyRule(Base):
    __tablename__ = "policy_rules"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_id: Mapped[str] = mapped_column(
        ForeignKey("policies.id"), nullable=False, index=True
    )
    tool: Mapped[str] = mapped_column(String(255), nullable=False)
    condition: Mapped[str] = mapped_column(Text, nullable=False)
    conditions: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expression: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    attributes: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)

    policy = relationship("Policy", back_populates="rules")

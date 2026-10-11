from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base


class ToolSet(Base):
    __tablename__ = "tool_sets"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_tool_sets_org_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    items = relationship(
        "ToolSetItem",
        back_populates="tool_set",
        cascade="all, delete-orphan",
    )


class ToolSetItem(Base):
    __tablename__ = "tool_set_items"
    __table_args__ = (
        UniqueConstraint("tool_set_id", "tool_id", name="uq_tool_set_items_tool"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tool_set_id: Mapped[str] = mapped_column(
        ForeignKey("tool_sets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_id: Mapped[str] = mapped_column(
        ForeignKey("tools.id", ondelete="CASCADE"), nullable=False, index=True
    )
    approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    tool_set = relationship("ToolSet", back_populates="items")
    tool = relationship("Tool")

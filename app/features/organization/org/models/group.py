from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_groups_org_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan")


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "employee_id", name="uq_group_members"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    employee_id: Mapped[str] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    group = relationship("Group", back_populates="members")

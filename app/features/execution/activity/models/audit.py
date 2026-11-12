from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base


class AuditCounter(Base):
    __tablename__ = "audit_counters"

    organization_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        # The activity list: one organization, newest first.
        Index("ix_audit_logs_org_created", "organization_id", "created_at", "id"),
        Index("ix_audit_logs_action_id", "action_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    organization_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("organizations.id"), nullable=True, index=True
    )
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    action_id: Mapped[Optional[str]] = mapped_column(ForeignKey("actions.id"), nullable=True)
    tool: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    result: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    content_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    organization = relationship("Organization", back_populates="audit_logs")
    run = relationship("Run", back_populates="audit_logs")

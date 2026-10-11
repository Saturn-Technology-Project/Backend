from __future__ import annotations

from datetime import datetime

from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    photo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email_mfa: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    organization = relationship("Organization", back_populates="users")
    employee = relationship("Employee", back_populates="user", uselist=False)
    sessions = relationship("AuthSession", back_populates="user")

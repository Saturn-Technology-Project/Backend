from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import utcnow
from app.db.database import Base


class OrgPayloadKey(Base):
    __tablename__ = "org_payload_keys"
    __table_args__ = (UniqueConstraint("organization_id", "version", name="uq_org_payload_key_version"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class OrgKeyBackup(Base):
    __tablename__ = "org_key_backups"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    format_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kdf: Mapped[str] = mapped_column(String(64), nullable=False)
    # 0 for the legacy HKDF format; the PBKDF2 iteration count for v2.
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    salt: Mapped[str] = mapped_column(String(128), nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MemberKeyBackup(Base):
    __tablename__ = "member_key_backups"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    member_key_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    format_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kdf: Mapped[str] = mapped_column(String(64), nullable=False)
    iterations: Mapped[int] = mapped_column(Integer, nullable=False)
    salt: Mapped[str] = mapped_column(String(128), nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MemberPayloadKey(Base):
    __tablename__ = "member_payload_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    # pending until a team.write admin approves it; only approved keys receive new DEK wraps.
    approval: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="approved")
    approved_by: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    assist_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MemberKeyEnvelope(Base):
    __tablename__ = "member_key_envelopes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    member_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    org_key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    wrapped_private_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class PayloadRotation(Base):
    __tablename__ = "payload_rotations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_version: Mapped[int] = mapped_column(Integer, nullable=False)
    to_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    migrated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

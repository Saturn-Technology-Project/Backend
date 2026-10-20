from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import utcnow
from app.db.database import Base


class OrgCredential(Base):
    """Credential ids are chosen by the client and are unique per organization, not globally."""

    __tablename__ = "org_credentials"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), primary_key=True, index=True
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    provider: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    model_name: Mapped[str] = mapped_column("model", String(128), default="", nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    secret_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    broker_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    allowed_paths: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    secret_ciphertext: Mapped[str] = mapped_column(Text, default="", nullable=False)
    vault_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

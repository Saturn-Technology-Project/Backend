from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (UniqueConstraint("agent_id", name="uq_wallets_agent_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False, index=True)
    address: Mapped[str] = mapped_column(String(66), nullable=False)
    network: Mapped[str] = mapped_column(String(64), default="base-sepolia", nullable=False)
    asset: Mapped[str] = mapped_column(String(16), default="USDC", nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"), nullable=False)
    daily_spend_limit: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), default=Decimal("10"), nullable=False
    )
    per_tx_limit: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), default=Decimal("1"), nullable=False
    )
    spent_today: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"), nullable=False)
    spend_reset_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    organization = relationship("Organization", back_populates="wallets")
    agent = relationship("Agent", back_populates="wallet")
    payments = relationship("Payment", back_populates="wallet")

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import utcnow
from app.db.database import Base


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    wallet_id: Mapped[str] = mapped_column(ForeignKey("wallets.id"), nullable=False, index=True)
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    action_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("actions.id"), nullable=True, unique=True, index=True
    )
    tool: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    asset: Mapped[str] = mapped_column(String(16), default="USDC", nullable=False)
    network: Mapped[str] = mapped_column(String(64), nullable=False)
    pay_to: Mapped[str] = mapped_column(String(66), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    tx_hash: Mapped[Optional[str]] = mapped_column(String(66), nullable=True)
    requirements: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    facilitator: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    wallet = relationship("Wallet", back_populates="payments")
    action = relationship("Action", back_populates="payment", uselist=False)

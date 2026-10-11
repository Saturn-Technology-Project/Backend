from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.ids import new_id, utcnow
from app.models import Agent, User, Wallet

settings = get_settings()


@dataclass(frozen=True)
class WalletAuthorization:
    allowed: bool
    reason: str | None
    amount: Decimal


def _address_for(agent_id: str) -> str:
    import hashlib

    return "0x" + hashlib.sha256(agent_id.encode("utf-8")).hexdigest()[:40]


class WalletService:
    async def list_wallets(self, session: AsyncSession, user: User) -> list[Wallet]:
        result = await session.execute(
            select(Wallet)
            .where(Wallet.organization_id == user.organization_id)
            .order_by(Wallet.created_at.asc())
        )
        wallets = list(result.scalars().all())
        for wallet in wallets:
            self.reset_daily_spend(wallet)
        return wallets

    async def get_wallet(self, session: AsyncSession, wallet_id: str) -> Wallet:
        wallet = await session.get(Wallet, wallet_id)
        if wallet is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")
        self.reset_daily_spend(wallet)
        return wallet

    async def get_wallet_for_agent(self, session: AsyncSession, agent_id: str) -> Wallet:
        result = await session.execute(select(Wallet).where(Wallet.agent_id == agent_id))
        wallet = result.scalar_one_or_none()
        if wallet is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")
        self.reset_daily_spend(wallet)
        return wallet

    async def ensure_wallet(
        self,
        session: AsyncSession,
        agent: Agent,
        *,
        balance: Decimal | None = None,
        wallet_id: str | None = None,
        address: str | None = None,
    ) -> Wallet:
        result = await session.execute(select(Wallet).where(Wallet.agent_id == agent.id))
        wallet = result.scalar_one_or_none()
        if wallet is not None:
            self.reset_daily_spend(wallet)
            return wallet
        wallet = Wallet(
            id=wallet_id or new_id("wallet"),
            organization_id=agent.organization_id,
            agent_id=agent.id,
            address=address or _address_for(agent.id),
            network=settings.x402_network,
            asset="USDC",
            balance=balance if balance is not None else Decimal("0"),
            daily_spend_limit=Decimal("10"),
            per_tx_limit=Decimal("1"),
            spent_today=Decimal("0"),
            spend_reset_on=utcnow().date(),
            status="active",
        )
        session.add(wallet)
        await session.flush()
        return wallet

    def update_limits(self, wallet: Wallet, *, daily: Decimal | None = None, per_tx: Decimal | None = None) -> None:
        if daily is not None:
            if daily <= 0:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid daily spend limit")
            wallet.daily_spend_limit = daily
        if per_tx is not None:
            if per_tx <= 0:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid per-transaction limit")
            wallet.per_tx_limit = per_tx
        wallet.updated_at = utcnow()

    def reset_daily_spend(self, wallet: Wallet) -> None:
        today = utcnow().date()
        if wallet.spend_reset_on != today:
            wallet.spent_today = Decimal("0")
            wallet.spend_reset_on = today

    def authorize(self, wallet: Wallet, amount: Decimal) -> WalletAuthorization:
        self.reset_daily_spend(wallet)
        if wallet.status != "active":
            return WalletAuthorization(False, "wallet_inactive", amount)
        if amount <= 0:
            return WalletAuthorization(False, "invalid_amount", amount)
        if amount > wallet.per_tx_limit:
            return WalletAuthorization(False, "over_per_tx_limit", amount)
        if wallet.spent_today + amount > wallet.daily_spend_limit:
            return WalletAuthorization(False, "over_daily_limit", amount)
        if amount > wallet.balance:
            return WalletAuthorization(False, "insufficient_balance", amount)
        return WalletAuthorization(True, None, amount)

    def debit(self, wallet: Wallet, amount: Decimal) -> None:
        wallet.balance = wallet.balance - amount
        wallet.spent_today = wallet.spent_today + amount
        wallet.updated_at = utcnow()


wallet_service = WalletService()

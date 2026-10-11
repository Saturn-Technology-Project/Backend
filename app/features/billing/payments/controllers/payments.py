from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.features.identity.auth.controllers.deps import AgentsRead, CurrentUser, SessionDep
from app.models import Payment
from app.schemas import PaymentRead

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("", response_model=list[PaymentRead])
async def list_payments(session: SessionDep, user: CurrentUser, _access: AgentsRead, run_id: str | None = None):
    stmt = select(Payment).order_by(Payment.created_at.desc())
    if run_id:
        stmt = stmt.where(Payment.run_id == run_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get("/{payment_id}", response_model=PaymentRead)
async def get_payment(payment_id: str, session: SessionDep, user: CurrentUser, _access: AgentsRead):
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    return payment

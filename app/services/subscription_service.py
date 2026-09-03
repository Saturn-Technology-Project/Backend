from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.ids import new_id, utcnow
from app.models.organization import Organization
from app.schemas import (
    SubscriptionInvoiceRead,
    SubscriptionLineRead,
    SubscriptionPaymentRead,
    SubscriptionRead,
)
from app.services.organization_service import public_addons

PLAN_CATALOG = {
    "trial": {"name": "Saturn Trial", "amount": "0.00"},
    "starter": {"name": "Saturn Starter", "amount": "290.00"},
    "enterprise": {"name": "Saturn Enterprise", "amount": "990.00"},
}
ADDON_AMOUNT = "200.00"
DEMO_PAYMENT = {"brand": "visa", "last4": "4242", "exp": "12/28", "demo": True}


def _money(value: Any) -> str:
    return f"{Decimal(str(value)).quantize(Decimal('0.01'))}"


def _month_start(moment: datetime) -> datetime:
    return datetime(moment.year, moment.month, 1, tzinfo=timezone.utc)


def _next_month(moment: datetime) -> datetime:
    if moment.month == 12:
        return datetime(moment.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(moment.year, moment.month + 1, 1, tzinfo=timezone.utc)


def _period_key(moment: datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = str(value or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def plan_line(plan: str) -> SubscriptionLineRead:
    ident = (plan or "trial").strip().lower()
    if ident not in PLAN_CATALOG:
        ident = "trial"
    item = PLAN_CATALOG[ident]
    return SubscriptionLineRead(kind="plan", id=ident, name=item["name"], amount=item["amount"])


def addon_lines(org: Organization) -> list[SubscriptionLineRead]:
    lines: list[SubscriptionLineRead] = []
    for addon in public_addons(org.addons):
        amount = ADDON_AMOUNT
        if isinstance(org.addons, list):
            raw = next(
                (
                    item
                    for item in org.addons
                    if isinstance(item, dict) and str(item.get("id") or "") == addon.id
                ),
                None,
            )
            if raw and raw.get("amount") is not None:
                amount = _money(raw.get("amount"))
        lines.append(
            SubscriptionLineRead(kind="addon", id=addon.id, name=addon.name, amount=amount)
        )
    return lines


def _invoice_from_lines(
    *,
    period: datetime,
    lines: list[SubscriptionLineRead],
    paid: bool,
) -> dict[str, Any]:
    total = sum((Decimal(line.amount) for line in lines), Decimal("0.00"))
    issued = period
    paid_at = None
    if paid:
        paid_at = (issued + timedelta(days=3)).isoformat()
    status_name = "paid" if paid or total == 0 else "open"
    if total == 0:
        paid_at = issued.isoformat()
        status_name = "paid"
    return {
        "id": new_id("inv"),
        "number": f"INV-{period.year:04d}-{period.month:02d}",
        "period": _period_key(period),
        "issued_at": issued.isoformat(),
        "paid_at": paid_at,
        "amount": _money(total),
        "currency": "USD",
        "status": status_name,
        "lines": [line.model_dump() for line in lines],
    }


def _read_invoice(raw: dict[str, Any]) -> SubscriptionInvoiceRead:
    return SubscriptionInvoiceRead(
        id=str(raw.get("id") or ""),
        number=str(raw.get("number") or ""),
        period=str(raw.get("period") or ""),
        issued_at=_parse_dt(raw.get("issued_at")) or utcnow(),
        paid_at=_parse_dt(raw.get("paid_at")),
        amount=_money(raw.get("amount") or "0"),
        currency=str(raw.get("currency") or "USD"),
        status=str(raw.get("status") or "open"),
        lines=[
            SubscriptionLineRead(
                kind=str(item.get("kind") or "plan"),
                id=str(item.get("id") or ""),
                name=str(item.get("name") or ""),
                amount=_money(item.get("amount") or "0"),
            )
            for item in (raw.get("lines") or [])
            if isinstance(item, dict)
        ],
    )


def _persist_billing(org: Organization, billing: dict[str, Any]) -> dict[str, Any]:
    stored = copy.deepcopy(billing)
    org.billing = stored
    flag_modified(org, "billing")
    return stored


def ensure_billing(org: Organization) -> dict[str, Any]:
    billing = copy.deepcopy(org.billing) if isinstance(org.billing, dict) else {}
    changed = False
    if not isinstance(billing.get("payment"), dict):
        billing["payment"] = dict(DEMO_PAYMENT)
        changed = True
    invoices = billing.get("invoices")
    if not isinstance(invoices, list) or not invoices:
        now = utcnow()
        current = _month_start(now)
        previous = _month_start(current - timedelta(days=1))
        lines = [plan_line(org.plan), *addon_lines(org)]
        billing["invoices"] = [
            _invoice_from_lines(period=previous, lines=lines, paid=True),
            _invoice_from_lines(period=current, lines=lines, paid=False),
        ]
        changed = True
        invoices = billing["invoices"]
    has_open = any(
        isinstance(item, dict) and str(item.get("status") or "") == "open"
        for item in invoices or []
    )
    if not has_open:
        lines = [plan_line(org.plan), *addon_lines(org)]
        total = sum((Decimal(line.amount) for line in lines), Decimal("0.00"))
        if total > 0:
            issued = [
                _parse_dt(item.get("issued_at"))
                for item in invoices or []
                if isinstance(item, dict)
            ]
            latest = max((item for item in issued if item is not None), default=utcnow())
            billing.setdefault("invoices", []).append(
                _invoice_from_lines(
                    period=_next_month(_month_start(latest)), lines=lines, paid=False
                )
            )
            changed = True
    if changed:
        return _persist_billing(org, billing)
    return billing


def serialize_subscription(org: Organization) -> SubscriptionRead:
    billing = ensure_billing(org)
    invoices = [
        _read_invoice(item)
        for item in billing.get("invoices") or []
        if isinstance(item, dict)
    ]
    invoices.sort(key=lambda item: item.issued_at, reverse=True)
    payment_raw = billing.get("payment") if isinstance(billing.get("payment"), dict) else {}
    now = utcnow()
    return SubscriptionRead(
        organization_id=org.id,
        organization_name=org.name,
        status=org.status or "active",
        plan=plan_line(org.plan),
        addons=addon_lines(org),
        renews_on=_next_month(_month_start(now)),
        payment=SubscriptionPaymentRead(
            brand=str(payment_raw.get("brand") or DEMO_PAYMENT["brand"]),
            last4=str(payment_raw.get("last4") or DEMO_PAYMENT["last4"]),
            exp=str(payment_raw.get("exp") or DEMO_PAYMENT["exp"]),
            demo=bool(payment_raw.get("demo", True)),
        ),
        invoices=invoices,
    )


async def get_subscription(session: AsyncSession, org: Organization) -> SubscriptionRead:
    payload = serialize_subscription(org)
    await session.flush()
    return payload


async def pay_invoice(session: AsyncSession, org: Organization, invoice_id: str) -> SubscriptionRead:
    billing = ensure_billing(org)
    invoices = billing.get("invoices") if isinstance(billing.get("invoices"), list) else []
    target = None
    for item in invoices:
        if isinstance(item, dict) and str(item.get("id") or "") == invoice_id:
            target = item
            break
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    if str(target.get("status") or "") == "paid":
        raise HTTPException(status.HTTP_409_CONFLICT, "Invoice already paid")
    now = utcnow()
    target["status"] = "paid"
    target["paid_at"] = now.isoformat()
    _persist_billing(org, billing)
    await session.flush()
    return serialize_subscription(org)

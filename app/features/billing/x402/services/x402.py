from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.ids import new_id, utcnow
from app.models import Payment, Tool, Wallet
from app.core.enums import ActorType, AuditEvent, PaymentStatus
from app.features.execution.activity.services.audit_service import audit_service
from app.features.billing.wallets.services.wallet_service import wallet_service

settings = get_settings()
USDC_DECIMALS = Decimal("1000000")


def dollars_to_atomic(amount: Decimal) -> str:
    return str(int((amount * USDC_DECIMALS).to_integral_value()))


def atomic_to_dollars(atomic: str) -> Decimal:
    return Decimal(atomic) / USDC_DECIMALS


def tool_price(tool: Tool) -> Decimal | None:
    config = tool.x402 or None
    if not config:
        return None
    return Decimal(str(config.get("price", "0")))


def payment_requirements(tool: Tool) -> dict[str, Any] | None:
    price = tool_price(tool)
    if price is None:
        return None
    config = tool.x402 or {}
    network = config.get("network") or settings.x402_network
    pay_to = config.get("pay_to") or settings.x402_pay_to
    asset = config.get("asset_address") or settings.x402_asset_address
    return {
        "x402Version": 1,
        "error": "X-PAYMENT header is required",
        "accepts": [
            {
                "scheme": config.get("scheme", "exact"),
                "network": network,
                "maxAmountRequired": dollars_to_atomic(price),
                "resource": tool.mcp_endpoint or f"mcp://{tool.name}",
                "description": tool.description,
                "mimeType": "application/json",
                "payTo": pay_to,
                "maxTimeoutSeconds": 60,
                "asset": asset,
                "extra": {"name": config.get("asset", "USDC"), "version": "2"},
            }
        ],
    }


def _accepts(requirements: dict[str, Any]) -> dict[str, Any]:
    accepts = requirements.get("accepts") or []
    if not accepts:
        raise ValueError("paymentRequirements.accepts is empty")
    return accepts[0]


def sign_payload(wallet: Wallet, requirements: dict[str, Any]) -> dict[str, Any]:
    accept = _accepts(requirements)
    nonce_seed = new_id("x402")
    nonce = "0x" + hashlib.sha256(nonce_seed.encode("utf-8")).hexdigest()
    raw = ":".join(
        [
            wallet.address,
            accept["payTo"],
            accept["maxAmountRequired"],
            nonce,
        ]
    )
    signature = "0x" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return {
        "x402Version": 1,
        "scheme": accept.get("scheme", "exact"),
        "network": accept.get("network"),
        "payload": {
            "signature": signature,
            "authorization": {
                "from": wallet.address,
                "to": accept["payTo"],
                "value": accept["maxAmountRequired"],
                "validAfter": "0",
                "validBefore": str(int(utcnow().timestamp()) + 60),
                "nonce": nonce,
            },
        },
    }


def verify_payment(payload: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
    try:
        accept = _accepts(requirements)
    except ValueError as exc:
        return {"isValid": False, "invalidReason": str(exc)}
    body = (payload or {}).get("payload") or {}
    authorization = body.get("authorization") or {}
    if not body.get("signature"):
        return {"isValid": False, "invalidReason": "missing_signature"}
    if payload.get("network") != accept.get("network"):
        return {"isValid": False, "invalidReason": "network_mismatch"}
    if authorization.get("value") != accept.get("maxAmountRequired"):
        return {"isValid": False, "invalidReason": "amount_mismatch"}
    if authorization.get("to") != accept.get("payTo"):
        return {"isValid": False, "invalidReason": "pay_to_mismatch"}
    if not authorization.get("from"):
        return {"isValid": False, "invalidReason": "missing_from"}
    return {"isValid": True, "invalidReason": None}


def settle_payment(payload: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
    checked = verify_payment(payload, requirements)
    if not checked["isValid"]:
        return {
            "success": False,
            "errorReason": checked["invalidReason"],
            "transaction": None,
            "network": None,
        }
    accept = _accepts(requirements)
    digest = hashlib.sha256(
        f"{payload['payload']['signature']}:{accept['maxAmountRequired']}".encode("utf-8")
    ).hexdigest()
    return {
        "success": True,
        "errorReason": None,
        "transaction": "0x" + digest,
        "network": accept.get("network"),
    }


class X402Service:
    async def pay_for_action(
        self,
        session: AsyncSession,
        *,
        action,
        tool: Tool,
        agent,
    ) -> Payment | None:
        requirements = payment_requirements(tool)
        if requirements is None:
            return None

        wallet = await wallet_service.ensure_wallet(session, agent)
        amount = tool_price(tool) or Decimal("0")
        payment = Payment(
            id=new_id("pay"),
            wallet_id=wallet.id,
            run_id=action.run_id,
            action_id=action.id,
            tool=tool.name,
            amount=amount,
            asset="USDC",
            network=_accepts(requirements)["network"],
            pay_to=_accepts(requirements)["payTo"],
            status=PaymentStatus.PENDING,
            requirements=requirements,
        )
        session.add(payment)
        await session.flush()
        await audit_service.log(
            session,
            event=AuditEvent.X402_PAYMENT_REQUIRED,
            actor_type=ActorType.SYSTEM,
            actor_id="x402",
            organization_id=agent.organization_id,
            run_id=action.run_id,
            action_id=action.id,
            tool=tool.name,
            result=str(amount),
            payload={"payment_id": payment.id, "requirements": requirements},
        )

        decision = wallet_service.authorize(wallet, amount)
        await audit_service.log(
            session,
            event=AuditEvent.WALLET_EVALUATED,
            actor_type=ActorType.WALLET,
            actor_id=wallet.id,
            organization_id=agent.organization_id,
            run_id=action.run_id,
            action_id=action.id,
            tool=tool.name,
            result="allow" if decision.allowed else "deny",
            payload={
                "reason": decision.reason,
                "amount": str(amount),
                "balance": str(wallet.balance),
            },
        )
        if not decision.allowed:
            payment.status = PaymentStatus.DENIED
            payment.facilitator = {"reason": decision.reason}
            await session.flush()
            await audit_service.log(
                session,
                event=AuditEvent.X402_FAILED,
                actor_type=ActorType.WALLET,
                actor_id=wallet.id,
                organization_id=agent.organization_id,
                run_id=action.run_id,
                action_id=action.id,
                tool=tool.name,
                result=decision.reason,
                payload={"payment_id": payment.id},
            )
            return payment

        payload = sign_payload(wallet, requirements)
        payment.payload = payload
        verified = verify_payment(payload, requirements)
        if not verified["isValid"]:
            payment.status = PaymentStatus.FAILED
            payment.facilitator = verified
            await session.flush()
            await audit_service.log(
                session,
                event=AuditEvent.X402_FAILED,
                actor_type=ActorType.SYSTEM,
                actor_id="x402",
                organization_id=agent.organization_id,
                run_id=action.run_id,
                action_id=action.id,
                tool=tool.name,
                result=verified["invalidReason"],
                payload={"payment_id": payment.id},
            )
            return payment

        settled = settle_payment(payload, requirements)
        payment.facilitator = {"verify": verified, "settle": settled}
        if not settled["success"]:
            payment.status = PaymentStatus.FAILED
            await session.flush()
            await audit_service.log(
                session,
                event=AuditEvent.X402_FAILED,
                actor_type=ActorType.SYSTEM,
                actor_id="x402",
                organization_id=agent.organization_id,
                run_id=action.run_id,
                action_id=action.id,
                tool=tool.name,
                result=settled["errorReason"],
                payload={"payment_id": payment.id},
            )
            return payment

        wallet_service.debit(wallet, amount)
        payment.status = PaymentStatus.SETTLED
        payment.tx_hash = settled["transaction"]
        payment.settled_at = utcnow()
        await session.flush()
        await audit_service.log(
            session,
            event=AuditEvent.X402_SETTLED,
            actor_type=ActorType.WALLET,
            actor_id=wallet.id,
            organization_id=agent.organization_id,
            run_id=action.run_id,
            action_id=action.id,
            tool=tool.name,
            result="success",
            payload={
                "payment_id": payment.id,
                "tx_hash": payment.tx_hash,
                "amount": str(amount),
            },
        )
        return payment


x402_service = X402Service()

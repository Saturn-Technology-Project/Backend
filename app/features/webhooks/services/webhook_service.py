from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id, utcnow
from app.features.credentials.vault.services.secret_vault import get_secret, put_secret
from app.features.organization.org.services.plans import PRO_REQUIRED, allows_pro
from app.models import Organization
from app.features.webhooks.models.webhook import WebhookAttempt, WebhookDelivery, WebhookEndpoint
from app.features.webhooks.services.destinations import require_webhook_url
from app.features.webhooks.services.signing import SCHEMA, canonical_body, signed_headers

ALLOWED_EVENTS = ("approval.granted", "approval.rejected", "approval.expired")
TIMEOUT_SECONDS = 5


def max_attempts() -> int:
    return 5


def backoff_seconds(attempt: int) -> float:
    return min(300, 2 ** min(attempt, 8)) + random.random()


def disabled_delay_seconds() -> int:
    return 60


def clean_events(events: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in events:
        if item not in ALLOWED_EVENTS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown webhook event")
        if item not in cleaned:
            cleaned.append(item)
    if not cleaned:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose at least one event")
    return cleaned


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    moment = _as_utc(value)
    if moment is None:
        return None
    return moment.isoformat()


def _public_error(text: str, secret: str) -> str:
    cleaned = (text or "").replace(secret, "••••") if secret else (text or "")
    return cleaned[:180]


async def send_webhook(url: str, body: bytes, headers: dict[str, str]) -> tuple[int | None, str]:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.post(url, content=body, headers=headers)
    except httpx.TimeoutException:
        return None, "timeout"
    except Exception as exc:
        return None, type(exc).__name__
    if 300 <= response.status_code < 400:
        return response.status_code, "redirect not followed"
    return response.status_code, ""


async def _webhooks_allowed(session: AsyncSession, organization_id: str) -> bool:
    org = await session.get(Organization, organization_id)
    return org is not None and allows_pro(org.plan)


async def _endpoint(session: AsyncSession, organization_id: str, endpoint_id: str) -> WebhookEndpoint:
    row = await session.get(WebhookEndpoint, endpoint_id)
    if row is None or row.organization_id != organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook endpoint not found")
    return row


async def list_endpoints(session: AsyncSession, organization_id: str) -> list[WebhookEndpoint]:
    result = await session.scalars(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.organization_id == organization_id)
        .order_by(WebhookEndpoint.created_at.desc())
    )
    return list(result.all())


async def create_endpoint(
    session: AsyncSession,
    organization_id: str,
    *,
    url: str,
    events: list[str],
) -> tuple[WebhookEndpoint, str]:
    checked = require_webhook_url(url)
    chosen = clean_events(events)
    import secrets

    secret = secrets.token_urlsafe(32)
    endpoint_id = new_id("wh")
    secret_id = f"whsec_{endpoint_id}"
    await put_secret(session, secret_id, secret, organization_id)
    row = WebhookEndpoint(
        id=endpoint_id,
        organization_id=organization_id,
        url=checked,
        events=chosen,
        enabled=True,
        secret_id=secret_id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    return row, secret


async def update_endpoint(
    session: AsyncSession,
    organization_id: str,
    endpoint_id: str,
    *,
    url: str | None = None,
    events: list[str] | None = None,
    enabled: bool | None = None,
) -> WebhookEndpoint:
    row = await _endpoint(session, organization_id, endpoint_id)
    if url is not None:
        row.url = require_webhook_url(url)
    if events is not None:
        row.events = clean_events(events)
    if enabled is not None:
        row.enabled = enabled
    row.updated_at = utcnow()
    await session.flush()
    return row


async def reveal_secret(session: AsyncSession, organization_id: str, endpoint_id: str) -> str:
    row = await _endpoint(session, organization_id, endpoint_id)
    from app.features.credentials.vault.services.pq_crypto import SecretNeedsReconnect

    try:
        secret = await get_secret(session, row.secret_id, organization_id)
    except SecretNeedsReconnect as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Signing secret needs to be rotated") from exc
    if not secret:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Signing secret is not available")
    return secret


async def rotate_secret(session: AsyncSession, organization_id: str, endpoint_id: str) -> str:
    import secrets

    row = await _endpoint(session, organization_id, endpoint_id)
    secret = secrets.token_urlsafe(32)
    await put_secret(session, row.secret_id, secret, organization_id)
    row.updated_at = utcnow()
    await session.flush()
    return secret


async def publish_approval(
    session: AsyncSession,
    *,
    organization_id: str,
    event_type: str,
    approval_id: str,
    run_id: str,
    action_id: str,
    tool: str,
    phase: str | None,
    expires_at: datetime | None,
    resubmit_expires_at: datetime | None,
    approval_status: str,
) -> None:
    key = f"{event_type}:{approval_id}"
    if phase:
        key = f"{key}:{phase}"
    data = {
        "approval_id": approval_id,
        "run_id": run_id,
        "action_id": action_id,
        "tool": tool,
        "status": approval_status,
        "phase": phase,
        "expires_at": _iso(expires_at),
        "resubmit_expires_at": _iso(resubmit_expires_at),
    }
    if not await _webhooks_allowed(session, organization_id):
        return
    await emit(session, organization_id, event_type, key, data)


async def emit(
    session: AsyncSession,
    organization_id: str,
    event_type: str,
    event_key: str,
    data: dict,
) -> None:
    endpoints = list(
        await session.scalars(
            select(WebhookEndpoint).where(
                WebhookEndpoint.organization_id == organization_id,
                WebhookEndpoint.enabled.is_(True),
            )
        )
    )
    for endpoint in endpoints:
        subscribed = list(endpoint.events or [])
        if event_type != "webhook.test" and event_type not in subscribed:
            continue
        event_id = new_id("whe")
        payload = {
            "schema": SCHEMA,
            "id": event_id,
            "type": event_type,
            "created_at": _iso(utcnow()),
            "data": data,
        }
        try:
            async with session.begin_nested():
                session.add(
                    WebhookDelivery(
                        id=new_id("whd"),
                        organization_id=organization_id,
                        endpoint_id=endpoint.id,
                        event_type=event_type,
                        event_key=event_key,
                        event_id=event_id,
                        payload=payload,
                        status="pending",
                        attempt_count=0,
                        next_attempt_at=utcnow(),
                        last_error="",
                        created_at=utcnow(),
                        updated_at=utcnow(),
                    )
                )
                await session.flush()
        except IntegrityError:
            continue


async def send_test(session: AsyncSession, organization_id: str, endpoint_id: str) -> WebhookDelivery:
    endpoint = await _endpoint(session, organization_id, endpoint_id)
    if not endpoint.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Endpoint is disabled")
    event_id = new_id("whe")
    payload = {
        "schema": SCHEMA,
        "id": event_id,
        "type": "webhook.test",
        "created_at": _iso(utcnow()),
        "data": {"note": "Saturn webhook test"},
    }
    row = WebhookDelivery(
        id=new_id("whd"),
        organization_id=organization_id,
        endpoint_id=endpoint.id,
        event_type="webhook.test",
        event_key=f"webhook.test:{event_id}",
        event_id=event_id,
        payload=payload,
        status="pending",
        attempt_count=0,
        next_attempt_at=utcnow(),
        last_error="",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    await deliver_one(session, row, utcnow())
    await session.refresh(row, attribute_names=["attempts"])
    return row


async def list_deliveries(
    session: AsyncSession,
    organization_id: str,
    endpoint_id: str,
) -> list[WebhookDelivery]:
    await _endpoint(session, organization_id, endpoint_id)
    result = await session.scalars(
        select(WebhookDelivery)
        .options(selectinload(WebhookDelivery.attempts))
        .where(
            WebhookDelivery.organization_id == organization_id,
            WebhookDelivery.endpoint_id == endpoint_id,
        )
        .order_by(WebhookDelivery.created_at.desc())
        .limit(50)
    )
    return list(result.all())


async def retry_delivery(session: AsyncSession, organization_id: str, delivery_id: str) -> WebhookDelivery:
    row = await session.scalar(
        select(WebhookDelivery)
        .options(selectinload(WebhookDelivery.attempts))
        .where(WebhookDelivery.id == delivery_id, WebhookDelivery.organization_id == organization_id)
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook delivery not found")
    if row.status == "delivered":
        raise HTTPException(status.HTTP_409_CONFLICT, "Delivery already succeeded")
    row.status = "pending"
    row.attempt_count = 0
    row.next_attempt_at = utcnow()
    row.last_error = ""
    await deliver_one(session, row, utcnow())
    await session.refresh(row, attribute_names=["attempts"])
    return row


def _retryable(code: int | None) -> bool:
    if code is None:
        return True
    if code == 429 or code >= 500:
        return True
    return False


async def deliver_due(session: AsyncSession, now: datetime | None = None) -> int:
    moment = now or utcnow()
    rows = list(
        await session.scalars(
            select(WebhookDelivery)
            .where(WebhookDelivery.status.in_(("pending", "failed")))
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(50)
        )
    )
    processed = 0
    for row in rows:
        if row.attempt_count >= max_attempts():
            row.status = "dead"
            row.updated_at = moment
            continue
        due = _as_utc(row.next_attempt_at)
        if due is not None and due > moment:
            continue
        await deliver_one(session, row, moment)
        processed += 1
        if processed >= 20:
            break
    return processed


async def deliver_one(session: AsyncSession, row: WebhookDelivery, now: datetime) -> None:
    endpoint = await session.get(WebhookEndpoint, row.endpoint_id)
    if endpoint is None or endpoint.organization_id != row.organization_id:
        row.status = "dead"
        row.last_error = "endpoint missing"
        row.updated_at = now
        return
    if not await _webhooks_allowed(session, row.organization_id):
        row.status = "dead"
        row.last_error = PRO_REQUIRED
        row.updated_at = now
        return
    if not endpoint.enabled and row.event_type != "webhook.test":
        row.next_attempt_at = now + timedelta(seconds=disabled_delay_seconds())
        row.updated_at = now
        return
    secret = ""
    try:
        require_webhook_url(endpoint.url)
        secret = await get_secret(session, endpoint.secret_id, row.organization_id)
    except HTTPException:
        await _finish(session, row, now, None, "destination not allowed", secret, dead=True)
        return
    except Exception:
        await _finish(session, row, now, None, "signing secret unavailable", "", dead=True)
        return
    if not secret:
        await _finish(session, row, now, None, "signing secret unavailable", "", dead=True)
        return
    body = canonical_body(row.payload if isinstance(row.payload, dict) else {})
    timestamp = str(int(_as_utc(now).timestamp()))
    headers = signed_headers(secret, row.event_id, timestamp, body)
    code, error = await send_webhook(endpoint.url, body, headers)
    dead = False
    if code is not None and 200 <= code < 300:
        row.status = "delivered"
        row.last_status_code = code
        row.last_error = ""
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.updated_at = now
        session.add(
            WebhookAttempt(
                id=new_id("wha"),
                delivery_id=row.id,
                status_code=code,
                error="",
                created_at=now,
            )
        )
        await session.flush()
        return
    if not _retryable(code):
        dead = True
    await _finish(session, row, now, code, error or (f"http {code}" if code else "delivery failed"), secret, dead=dead)


async def _finish(
    session: AsyncSession,
    row: WebhookDelivery,
    now: datetime,
    code: int | None,
    error: str,
    secret: str,
    *,
    dead: bool,
) -> None:
    row.attempt_count = int(row.attempt_count or 0) + 1
    row.last_status_code = code
    row.last_error = _public_error(error, secret)
    row.updated_at = now
    if dead or row.attempt_count >= max_attempts():
        row.status = "dead"
    else:
        row.status = "failed"
        row.next_attempt_at = now + timedelta(seconds=backoff_seconds(row.attempt_count))
    session.add(
        WebhookAttempt(
            id=new_id("wha"),
            delivery_id=row.id,
            status_code=code,
            error=row.last_error,
            created_at=now,
        )
    )
    await session.flush()

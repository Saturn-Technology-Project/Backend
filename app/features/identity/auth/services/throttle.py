from __future__ import annotations

import hashlib
from datetime import timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id, utcnow
from app.features.identity.auth.models.throttle import AuthThrottle

EMAIL_LIMIT = 1
EMAIL_WINDOW = timedelta(seconds=60)
CLIENT_LIMIT = 8
CLIENT_WINDOW = timedelta(minutes=10)
_WAIT = "Wait a minute before requesting another code"


def client_ip(request: Request) -> str:
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return cf_ip
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded.strip():
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


def _device(request: Request) -> str:
    raw = f"{client_ip(request)}\n{request.headers.get('user-agent', '')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def _hit(session: AsyncSession, bucket: str, *, limit: int, window: timedelta) -> None:
    cutoff = utcnow() - window
    count = await session.scalar(
        select(func.count())
        .select_from(AuthThrottle)
        .where(AuthThrottle.bucket == bucket, AuthThrottle.created_at >= cutoff)
    )
    if int(count or 0) >= limit:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, _WAIT)
    session.add(AuthThrottle(id=new_id("thr"), bucket=bucket))
    await session.flush()


async def consume(session: AsyncSession, request: Request, purpose: str, email: str) -> None:
    ip = client_ip(request)
    device = _device(request)
    await _hit(session, f"email:{purpose}:{email}", limit=EMAIL_LIMIT, window=EMAIL_WINDOW)
    await _hit(session, f"ip:{purpose}:{ip}", limit=CLIENT_LIMIT, window=CLIENT_WINDOW)
    await _hit(session, f"device:{purpose}:{device}", limit=CLIENT_LIMIT, window=CLIENT_WINDOW)

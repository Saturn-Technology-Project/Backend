from __future__ import annotations

import hashlib
from datetime import timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id, utcnow
from app.core.rate_limit import client_ip
from app.features.identity.auth.models.throttle import AuthThrottle

EMAIL_LIMIT = 1
EMAIL_WINDOW = timedelta(seconds=60)
CLIENT_LIMIT = 8
CLIENT_WINDOW = timedelta(minutes=10)
# Each code allows 5 guesses and a new one every minute, so without a daily cap one address
# could be guessed against around the clock.
EMAIL_DAILY_LIMIT = 10
EMAIL_DAILY_WINDOW = timedelta(days=1)
_WAIT = "Wait a minute before requesting another code"


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
    await _hit(session, f"email-day:{purpose}:{email}", limit=EMAIL_DAILY_LIMIT, window=EMAIL_DAILY_WINDOW)
    await _hit(session, f"ip:{purpose}:{ip}", limit=CLIENT_LIMIT, window=CLIENT_WINDOW)
    await _hit(session, f"device:{purpose}:{device}", limit=CLIENT_LIMIT, window=CLIENT_WINDOW)
    await maybe_purge(session)


_RETAIN = timedelta(days=1)


async def purge_expired(session: AsyncSession) -> None:
    """Drop throttle rows and spent sign-in codes. Without this the tables grow forever."""
    from sqlalchemy import delete

    from app.features.identity.auth.models.mfa import EmailCode, LoginChallenge, WebAuthnChallenge
    from app.features.identity.auth.models.password_reset import PasswordReset

    cutoff = utcnow() - _RETAIN
    await session.execute(delete(AuthThrottle).where(AuthThrottle.created_at < cutoff))
    for model in (EmailCode, LoginChallenge, WebAuthnChallenge, PasswordReset):
        await session.execute(delete(model).where(model.expires_at < cutoff))
    await session.flush()


async def maybe_purge(session: AsyncSession) -> None:
    import secrets

    if secrets.randbelow(64) == 0:
        await purge_expired(session)

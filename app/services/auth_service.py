from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.ids import new_id, utcnow
from app.models import Organization, User
from app.models.session import AuthSession

settings = get_settings()
PBKDF2_ROUNDS = 210_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PBKDF2_ROUNDS,
    )
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    check = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        int(rounds),
    )
    return hmac.compare_digest(check.hex(), digest)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    async def login(self, session: AsyncSession, email: str, password: str) -> tuple[str, User]:
        normalized = email.strip().lower()
        result = await session.execute(
            select(User)
            .options(selectinload(User.organization))
            .where(User.email == normalized)
        )
        user = result.scalar_one_or_none()
        if (
            user is None
            or user.status != "active"
            or not verify_password(password, user.password_hash)
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

        return await self.issue_session(session, user)

    async def issue_session(self, session: AsyncSession, user: User) -> tuple[str, User]:
        raw = "sat_" + secrets.token_urlsafe(32)
        record = AuthSession(
            id=new_id("sess"),
            user_id=user.id,
            token_hash=hash_token(raw),
            expires_at=utcnow() + timedelta(days=settings.session_ttl_days),
        )
        session.add(record)
        await session.flush()
        if user.organization is None:
            result = await session.execute(
                select(User).options(selectinload(User.organization)).where(User.id == user.id)
            )
            user = result.scalar_one()
        return raw, user

    async def logout(self, session: AsyncSession, token: str) -> None:
        result = await session.execute(
            select(AuthSession).where(AuthSession.token_hash == hash_token(token))
        )
        record = result.scalar_one_or_none()
        if record is None or record.revoked_at is not None:
            return
        record.revoked_at = utcnow()
        await session.flush()

    async def user_from_token(self, session: AsyncSession, token: str) -> User | None:
        result = await session.execute(
            select(AuthSession)
            .options(selectinload(AuthSession.user).selectinload(User.organization))
            .where(AuthSession.token_hash == hash_token(token))
        )
        record = result.scalar_one_or_none()
        if record is None:
            return None
        expires = record.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if (
            record.revoked_at is not None
            or expires <= utcnow()
            or record.user is None
            or record.user.status != "active"
        ):
            return None
        return record.user

    async def ensure_seed_user(self, session: AsyncSession) -> None:
        existing = await session.get(User, settings.default_actor_id)
        if existing is not None:
            if not existing.is_platform_admin:
                existing.is_platform_admin = True
                await session.flush()
            return
        org = await session.get(Organization, settings.default_org_id)
        if org is None:
            return
        session.add(
            User(
                id=settings.default_actor_id,
                organization_id=org.id,
                email=settings.seed_user_email.strip().lower(),
                name=settings.seed_user_name,
                password_hash=hash_password(settings.seed_user_password),
                status="active",
                is_platform_admin=True,
            )
        )
        await session.flush()


auth_service = AuthService()

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
from app.models import Organization, PasswordReset, User
from app.features.identity.auth.models.session import AuthSession

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

    async def update_profile(self, session: AsyncSession, user: User, *, name: str, email: str) -> User:
        cleaned = name.strip()
        if not cleaned or len(cleaned) > 255:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name is required")
        normalized = email.strip().lower()
        domain = normalized.split("@")[-1] if "@" in normalized else ""
        if not domain or "." not in domain or " " in normalized:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email is invalid")
        if normalized != user.email:
            existing = await session.execute(select(User).where(User.email == normalized))
            other = existing.scalar_one_or_none()
            if other is not None and other.id != user.id:
                raise HTTPException(status.HTTP_409_CONFLICT, "Email is already used")
            user.email = normalized
        user.name = cleaned
        await session.flush()
        return user

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

    async def revoke_user_sessions(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        keep_hash: str | None = None,
    ) -> None:
        result = await session.execute(
            select(AuthSession).where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
            )
        )
        now = utcnow()
        for record in result.scalars():
            if keep_hash is not None and record.token_hash == keep_hash:
                continue
            record.revoked_at = now
        await session.flush()

    async def request_password_reset(self, session: AsyncSession, email: str) -> str | None:
        normalized = email.strip().lower()
        recent = await session.execute(
            select(PasswordReset)
            .where(PasswordReset.email == normalized)
            .order_by(PasswordReset.created_at.desc())
            .limit(1)
        )
        last = recent.scalar_one_or_none()
        if last is not None:
            created = last.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if utcnow() - created < timedelta(seconds=60):
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "Wait a minute before requesting another code",
                )
        user = await self._active_user(session, normalized)
        if user is None:
            return None
        open_codes = await session.execute(
            select(PasswordReset).where(
                PasswordReset.email == normalized,
                PasswordReset.used_at.is_(None),
            )
        )
        now = utcnow()
        for record in open_codes.scalars():
            record.used_at = now
        code = f"{secrets.randbelow(1_000_000):06d}"
        session.add(
            PasswordReset(
                id=new_id("rst"),
                email=normalized,
                code_hash=hash_password(code),
                expires_at=now + timedelta(minutes=10),
                attempts=0,
            )
        )
        await session.flush()
        return code

    async def discard_password_reset(self, session: AsyncSession, email: str) -> None:
        normalized = email.strip().lower()
        result = await session.execute(
            select(PasswordReset)
            .where(PasswordReset.email == normalized, PasswordReset.used_at.is_(None))
            .order_by(PasswordReset.created_at.desc())
            .limit(1)
        )
        record = result.scalar_one_or_none()
        if record is not None:
            await session.delete(record)
            await session.flush()

    async def reset_password(
        self,
        session: AsyncSession,
        email: str,
        code: str,
        password: str,
    ) -> None:
        self._require_password(password)
        normalized = email.strip().lower()
        result = await session.execute(
            select(PasswordReset)
            .where(PasswordReset.email == normalized, PasswordReset.used_at.is_(None))
            .order_by(PasswordReset.created_at.desc())
            .limit(1)
        )
        record = result.scalar_one_or_none()
        now = utcnow()
        expires = record.expires_at if record is not None else None
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if record is None or expires is None or expires <= now or record.attempts >= 5:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Verification code is incorrect or expired")
        if not verify_password(code.strip(), record.code_hash):
            record.attempts += 1
            if record.attempts >= 5:
                record.used_at = now
            await session.flush()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Verification code is incorrect or expired")
        user = await self._active_user(session, normalized)
        if user is None:
            record.used_at = now
            await session.flush()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Verification code is incorrect or expired")
        user.password_hash = hash_password(password)
        record.used_at = now
        await self.revoke_user_sessions(session, user.id)

    async def change_password(
        self,
        session: AsyncSession,
        user: User,
        current_password: str,
        new_password: str,
        *,
        keep_token: str | None,
    ) -> None:
        if not verify_password(current_password, user.password_hash):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Current password is incorrect")
        self._require_password(new_password)
        user.password_hash = hash_password(new_password)
        keep_hash = hash_token(keep_token) if keep_token else None
        await self.revoke_user_sessions(session, user.id, keep_hash=keep_hash)

    async def _active_user(self, session: AsyncSession, email: str) -> User | None:
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user is None or user.status != "active":
            return None
        return user

    @staticmethod
    def _require_password(password: str) -> None:
        if len(password) < 8:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password must be at least 8 characters")

    async def ensure_seed_user(self, session: AsyncSession) -> None:
        existing = await session.get(User, settings.default_actor_id)
        if existing is not None:
            if not existing.is_platform_admin:
                existing.is_platform_admin = True
                await session.flush()
            return
        email = (settings.seed_user_email or "").strip().lower()
        password = settings.seed_user_password or ""
        if not email or not password:
            return
        org = await session.get(Organization, settings.default_org_id)
        if org is None:
            return
        session.add(
            User(
                id=settings.default_actor_id,
                organization_id=org.id,
                email=email,
                name=(settings.seed_user_name or "Admin").strip() or "Admin",
                password_hash=hash_password(password),
                status="active",
                is_platform_admin=True,
            )
        )
        await session.flush()


auth_service = AuthService()

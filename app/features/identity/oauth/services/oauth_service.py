from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ids import new_id, utcnow
from app.models import User
from app.features.identity.oauth.models.oauth import OAuthAccess, OAuthClient, OAuthGrant
from app.features.identity.auth.services.auth_service import hash_token

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
CODE_TTL = timedelta(minutes=5)
SCOPE = "saturn.mcp"


def _aware(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def redirect_allowed(uri: str) -> bool:
    parsed = urlparse(uri)
    if parsed.fragment or not parsed.scheme or parsed.scheme in {"javascript", "data", "file", "blob"}:
        return False
    if parsed.scheme == "https":
        return bool(parsed.hostname)
    if parsed.scheme == "http":
        return (parsed.hostname or "").lower() in {"localhost", "127.0.0.1"}
    return bool(parsed.netloc or parsed.path)


class OAuthService:
    async def register(
        self,
        session: AsyncSession,
        *,
        name: str,
        redirect_uris: list[str],
    ) -> OAuthClient:
        client = OAuthClient(
            id="oac_" + secrets.token_urlsafe(16),
            name=name.strip()[:255],
            redirect_uris=redirect_uris,
        )
        session.add(client)
        await session.flush()
        return client

    async def client_for(self, session: AsyncSession, client_id: str) -> OAuthClient | None:
        if not client_id:
            return None
        return await session.get(OAuthClient, client_id)

    def matching_redirect(self, client: OAuthClient, redirect_uri: str) -> bool:
        return redirect_uri in list(client.redirect_uris or [])

    async def issue_code(
        self,
        session: AsyncSession,
        *,
        client: OAuthClient,
        user: User,
        redirect_uri: str,
        code_challenge: str,
    ) -> str:
        raw = "oacode_" + secrets.token_urlsafe(32)
        session.add(
            OAuthGrant(
                id=new_id("grant"),
                code_hash=hash_token(raw),
                client_id=client.id,
                user_id=user.id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                expires_at=utcnow() + CODE_TTL,
            )
        )
        await session.flush()
        return raw

    async def exchange_code(
        self,
        session: AsyncSession,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> tuple[str, str, int] | None:
        result = await session.execute(
            select(OAuthGrant).where(OAuthGrant.code_hash == hash_token(code))
        )
        grant = result.scalar_one_or_none()
        if grant is None or grant.used_at is not None or grant.client_id != client_id:
            return None
        if grant.redirect_uri != redirect_uri or _aware(grant.expires_at) <= utcnow():
            return None
        expected = pkce_s256(code_verifier)
        presented = grant.code_challenge.strip().rstrip("=")
        if not code_verifier or len(expected) != len(presented) or not hmac.compare_digest(expected, presented):
            return None
        grant.used_at = utcnow()
        return await self._issue_tokens(session, client_id=client_id, user_id=grant.user_id)

    async def refresh(
        self,
        session: AsyncSession,
        *,
        refresh_token: str,
        client_id: str,
    ) -> tuple[str, str, int] | None:
        result = await session.execute(
            select(OAuthAccess).where(OAuthAccess.refresh_hash == hash_token(refresh_token))
        )
        current = result.scalar_one_or_none()
        if (
            current is None
            or current.revoked_at is not None
            or current.client_id != client_id
            or _aware(current.refresh_expires_at) <= utcnow()
        ):
            return None
        current.revoked_at = utcnow()
        return await self._issue_tokens(session, client_id=client_id, user_id=current.user_id)

    async def revoke_user_tokens(self, session: AsyncSession, user_id: str) -> None:
        now = utcnow()
        access = await session.execute(
            select(OAuthAccess).where(
                OAuthAccess.user_id == user_id,
                OAuthAccess.revoked_at.is_(None),
            )
        )
        for record in access.scalars():
            record.revoked_at = now
        grants = await session.execute(
            select(OAuthGrant).where(
                OAuthGrant.user_id == user_id,
                OAuthGrant.used_at.is_(None),
            )
        )
        for grant in grants.scalars():
            grant.used_at = now
        await session.flush()

    async def user_from_access_token(
        self,
        session: AsyncSession,
        token: str,
    ) -> tuple[User, str, str] | None:
        if not token.startswith("sat_oa_"):
            return None
        result = await session.execute(
            select(OAuthAccess).where(OAuthAccess.access_hash == hash_token(token))
        )
        record = result.scalar_one_or_none()
        if record is None or record.revoked_at is not None or _aware(record.expires_at) <= utcnow():
            return None
        user_result = await session.execute(
            select(User).options(selectinload(User.organization)).where(User.id == record.user_id)
        )
        user = user_result.scalar_one_or_none()
        if user is None or user.status != "active":
            return None
        client = await session.get(OAuthClient, record.client_id)
        return user, (client.name if client is not None else "Agent"), record.client_id

    async def _issue_tokens(
        self,
        session: AsyncSession,
        *,
        client_id: str,
        user_id: str,
    ) -> tuple[str, str, int]:
        access = "sat_oa_" + secrets.token_urlsafe(32)
        refresh = "sat_or_" + secrets.token_urlsafe(32)
        session.add(
            OAuthAccess(
                id=new_id("oat"),
                access_hash=hash_token(access),
                refresh_hash=hash_token(refresh),
                client_id=client_id,
                user_id=user_id,
                expires_at=utcnow() + ACCESS_TTL,
                refresh_expires_at=utcnow() + REFRESH_TTL,
            )
        )
        await session.flush()
        return access, refresh, int(ACCESS_TTL.total_seconds())


oauth_service = OAuthService()

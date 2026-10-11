from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.core.config import get_settings
from app.core.ids import new_id, utcnow
from app.features.identity.auth.models.mfa import Passkey, WebAuthnChallenge
from app.features.identity.auth.services.mfa import _as_utc, mfa_service
from app.models import User

CHALLENGE_TTL = timedelta(minutes=5)
settings = get_settings()


def relying_party(request: Request) -> tuple[str, str]:
    origin = request.headers.get("origin", "").rstrip("/")
    allowed = {item.strip().rstrip("/") for item in settings.cors_origins.split(",") if item.strip()}
    host = urlparse(origin).hostname if origin in allowed else None
    if not host:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This browser cannot use a passkey")
    return host, origin


class PasskeyService:
    async def list_passkeys(self, session: AsyncSession, user: User) -> list[Passkey]:
        result = await session.execute(
            select(Passkey).where(Passkey.user_id == user.id).order_by(Passkey.created_at.asc())
        )
        return list(result.scalars())

    async def registration_options(self, session: AsyncSession, request: Request, user: User) -> dict:
        rp_id, _origin = relying_party(request)
        existing = await self.list_passkeys(session, user)
        options = generate_registration_options(
            rp_id=rp_id,
            rp_name="Saturn",
            user_name=user.email,
            user_id=user.id.encode(),
            user_display_name=user.name,
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(item.credential_id)) for item in existing
            ],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
        )
        await self._store_challenge(session, user.id, "register", options.challenge)
        return json.loads(options_to_json(options))

    async def register(self, session: AsyncSession, request: Request, user: User, credential: dict) -> Passkey:
        rp_id, origin = relying_party(request)
        challenge = await self._take_challenge(session, user.id, "register")
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=base64url_to_bytes(challenge),
                expected_rp_id=rp_id,
                expected_origin=origin,
            )
        except WebAuthnException as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Passkey could not be verified") from exc
        credential_id = bytes_to_base64url(verified.credential_id)
        taken = await session.execute(select(Passkey).where(Passkey.credential_id == credential_id))
        if taken.scalar_one_or_none() is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "This passkey is already registered")
        record = Passkey(
            id=new_id("pkey"),
            user_id=user.id,
            credential_id=credential_id,
            public_key=bytes_to_base64url(verified.credential_public_key),
            sign_count=verified.sign_count,
        )
        session.add(record)
        await session.flush()
        return record

    async def delete(self, session: AsyncSession, user: User, passkey_id: str) -> None:
        result = await session.execute(
            select(Passkey).where(Passkey.id == passkey_id, Passkey.user_id == user.id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Passkey not found")
        await session.delete(record)
        await session.flush()

    async def reauth_options(self, session: AsyncSession, request: Request, user: User) -> dict:
        rp_id, _origin = relying_party(request)
        records = await self.list_passkeys(session, user)
        if not records:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Passkey could not be verified")
        options = generate_authentication_options(
            rp_id=rp_id,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(item.credential_id)) for item in records
            ],
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        await self._store_challenge(session, user.id, "reauth", options.challenge)
        return json.loads(options_to_json(options))

    async def reauth_verify(self, session: AsyncSession, request: Request, user: User, credential: dict) -> None:
        rp_id, origin = relying_party(request)
        credential_id = credential.get("id") if isinstance(credential, dict) else None
        if not isinstance(credential_id, str) or not credential_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Passkey could not be verified")
        result = await session.execute(
            select(Passkey).where(Passkey.credential_id == credential_id, Passkey.user_id == user.id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Passkey could not be verified")
        challenge = await self._take_challenge(session, user.id, "reauth")
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=base64url_to_bytes(challenge),
                expected_rp_id=rp_id,
                expected_origin=origin,
                credential_public_key=base64url_to_bytes(record.public_key),
                credential_current_sign_count=record.sign_count,
            )
        except WebAuthnException as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Passkey could not be verified") from exc
        record.sign_count = verified.new_sign_count
        await session.flush()

    async def authentication_options(self, session: AsyncSession, request: Request, token: str) -> dict:
        rp_id, _origin = relying_party(request)
        user = await mfa_service.user_for_challenge(session, token)
        records = await self.list_passkeys(session, user)
        if not records:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Passkey could not be verified")
        options = generate_authentication_options(
            rp_id=rp_id,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(item.credential_id)) for item in records
            ],
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        await self._store_challenge(session, user.id, "login", options.challenge)
        return json.loads(options_to_json(options))

    async def authenticate(self, session: AsyncSession, request: Request, token: str, credential: dict) -> User:
        rp_id, origin = relying_party(request)
        user = await mfa_service.user_for_challenge(session, token)
        credential_id = credential.get("id") if isinstance(credential, dict) else None
        if not isinstance(credential_id, str) or not credential_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Passkey could not be verified")
        result = await session.execute(
            select(Passkey).where(Passkey.credential_id == credential_id, Passkey.user_id == user.id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Passkey could not be verified")
        challenge = await self._take_challenge(session, user.id, "login")
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=base64url_to_bytes(challenge),
                expected_rp_id=rp_id,
                expected_origin=origin,
                credential_public_key=base64url_to_bytes(record.public_key),
                credential_current_sign_count=record.sign_count,
            )
        except WebAuthnException as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Passkey could not be verified") from exc
        record.sign_count = verified.new_sign_count
        return await mfa_service.finish_passkey(session, token)

    async def _store_challenge(
        self,
        session: AsyncSession,
        user_id: str,
        purpose: str,
        challenge: bytes,
    ) -> None:
        now = utcnow()
        open_rows = await session.execute(
            select(WebAuthnChallenge).where(
                WebAuthnChallenge.user_id == user_id,
                WebAuthnChallenge.purpose == purpose,
                WebAuthnChallenge.used_at.is_(None),
            )
        )
        for row in open_rows.scalars():
            row.used_at = now
        session.add(
            WebAuthnChallenge(
                id=new_id("wac"),
                user_id=user_id,
                purpose=purpose,
                challenge=bytes_to_base64url(challenge),
                expires_at=now + CHALLENGE_TTL,
            )
        )
        await session.flush()

    async def _take_challenge(self, session: AsyncSession, user_id: str, purpose: str) -> str:
        result = await session.execute(
            select(WebAuthnChallenge)
            .where(
                WebAuthnChallenge.user_id == user_id,
                WebAuthnChallenge.purpose == purpose,
                WebAuthnChallenge.used_at.is_(None),
            )
            .order_by(WebAuthnChallenge.created_at.desc())
            .limit(1)
        )
        record = result.scalar_one_or_none()
        if record is None or _as_utc(record.expires_at) <= utcnow():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Passkey could not be verified")
        record.used_at = utcnow()
        await session.flush()
        return record.challenge


passkey_service = PasskeyService()

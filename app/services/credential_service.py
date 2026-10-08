from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.ids import new_id, utcnow
from app.models.credential import OrgCredential
from app.models.user import User
from app.schemas import CredentialWrite

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KINDS = {"model", "key"}


def _key() -> bytes:
    raw = os.environ.get("SATURN_CREDENTIAL_KEY") or get_settings().database_url
    return hashlib.sha256(f"saturn-org-credential:{raw}".encode()).digest()


def _legacy_seal(plain: str) -> str:
    if not plain:
        return ""
    nonce = os.urandom(16)
    data = plain.encode()
    cipher = bytes(left ^ right for left, right in zip(data, _stream(nonce, len(data))))
    mac = hmac.new(_key(), nonce + cipher, hashlib.sha256).digest()
    return base64.b64encode(nonce + mac + cipher).decode()


def _legacy_open(token: str) -> str:
    if not token or token.startswith("pq1."):
        return ""
    try:
        raw = base64.b64decode(token)
    except Exception:
        return ""
    if len(raw) < 48:
        return ""
    nonce, mac, cipher = raw[:16], raw[16:48], raw[48:]
    expected = hmac.new(_key(), nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        return ""
    plain = bytes(left ^ right for left, right in zip(cipher, _stream(nonce, len(cipher))))
    try:
        return plain.decode()
    except UnicodeDecodeError:
        return ""


def _stream(nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    key = _key()
    while len(out) < length:
        out.extend(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def seal(plain: str) -> str:
    from app.services.pq_crypto import seal as pq_seal

    return pq_seal(plain)


def open_secret(token: str) -> str:
    from app.services.pq_crypto import open_secret as pq_open

    if not token:
        return ""
    if token.startswith("pq1."):
        return pq_open(token)
    return _legacy_open(token)


def _clip(value: str, limit: int) -> str:
    return value.strip()[:limit]


class CredentialService:
    async def list_credentials(self, session: AsyncSession, organization_id: str) -> list[OrgCredential]:
        result = await session.scalars(
            select(OrgCredential)
            .where(OrgCredential.organization_id == organization_id)
            .order_by(OrgCredential.created_at)
        )
        return list(result.all())

    async def upsert(self, session: AsyncSession, user: User, body: CredentialWrite) -> OrgCredential:
        kind = body.kind.strip()
        if kind not in _KINDS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Credential kind must be model or key")
        name = _clip(body.name, 255)
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name is required")
        secret_name = _clip(body.secret_name, 128)
        if kind == "key" and not secret_name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "secret_name is required")
        if kind == "model" and not _clip(body.model, 128):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Model is required")
        credential_id = body.id.strip() if body.id else ""
        if credential_id and not _ID.match(credential_id):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid credential id")
        current = await session.get(OrgCredential, credential_id) if credential_id else None
        if current is not None and current.organization_id != user.organization_id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Credential id is already used")
        if current is None:
            current = OrgCredential(
                id=credential_id or new_id("crd"),
                organization_id=user.organization_id,
                kind=kind,
                created_at=utcnow(),
            )
            session.add(current)
        current.kind = kind
        current.name = name
        current.provider = _clip(body.provider, 64)
        current.model_name = _clip(body.model, 128)
        current.base_url = _clip(body.base_url, 512)
        current.secret_name = secret_name or ("AGENTS_API_KEY" if kind == "model" else "")
        current.broker_id = _clip(body.broker_id, 64) or current.id
        current.allowed_paths = [str(item).strip() for item in body.allowed_paths if str(item).strip()]
        if body.secret is not None:
            from app.services.secret_vault import delete_secret, put_secret

            vault_id = f"org-{current.id}"
            if body.secret:
                await put_secret(session, vault_id, body.secret)
                current.vault_id = vault_id
            else:
                await delete_secret(session, current.vault_id or vault_id)
                current.vault_id = ""
            current.secret_ciphertext = ""
        current.updated_at = utcnow()
        await session.flush()
        return current

    async def delete(self, session: AsyncSession, user: User, credential_id: str) -> None:
        current = await session.get(OrgCredential, credential_id)
        if current is None or current.organization_id != user.organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Credential not found")
        from app.services.secret_vault import delete_secret

        await delete_secret(session, current.vault_id)
        await session.delete(current)


credential_service = CredentialService()

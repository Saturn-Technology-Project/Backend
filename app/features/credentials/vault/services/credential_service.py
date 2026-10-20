from __future__ import annotations

import hashlib
import re

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id, utcnow
from app.features.credentials.vault.models.credential import OrgCredential
from app.features.identity.auth.models.user import User
from app.schemas import CredentialWrite

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KINDS = {"model", "key"}


def seal(plain: str, organization_id: str, user_id: str = "") -> str:
    from app.features.credentials.vault.services.pq_crypto import seal as pq_seal

    return pq_seal(plain, organization_id, user_id)


def open_secret(token: str, organization_id: str, user_id: str = "") -> str:
    from app.features.credentials.vault.services.pq_crypto import open_secret as pq_open

    return pq_open(token, organization_id, user_id)


def _clip(value: str, limit: int) -> str:
    return value.strip()[:limit]


def vault_id_for(organization_id: str, credential_id: str) -> str:
    """Credential ids are per organization, so the vault entry name carries the organization too."""
    org_tag = hashlib.sha256(f"saturn-credential-org\n{organization_id}".encode()).hexdigest()[:16]
    return f"org-{org_tag}-{credential_id}"


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
        # Ids are scoped to the organization: another tenant using the same id is invisible here.
        current = await self._get(session, user.organization_id, credential_id) if credential_id else None
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
            from app.features.credentials.vault.services.secret_vault import delete_secret, put_secret

            vault_id = vault_id_for(user.organization_id, current.id)
            if body.secret:
                await put_secret(session, vault_id, body.secret, user.organization_id)
                if current.vault_id and current.vault_id != vault_id:
                    # The old entry ("org-<id>", from when ids were global) belonged to this row.
                    await delete_secret(session, current.vault_id, user.organization_id)
                current.vault_id = vault_id
            else:
                await delete_secret(session, current.vault_id or vault_id, user.organization_id)
                current.vault_id = ""
            current.secret_ciphertext = ""
        current.updated_at = utcnow()
        await session.flush()
        return current

    async def _get(self, session: AsyncSession, organization_id: str, credential_id: str) -> OrgCredential | None:
        return await session.get(OrgCredential, {"organization_id": organization_id, "id": credential_id})

    async def delete(self, session: AsyncSession, user: User, credential_id: str) -> None:
        current = await self._get(session, user.organization_id, credential_id)
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Credential not found")
        from app.features.credentials.vault.services.secret_vault import delete_secret

        await delete_secret(session, current.vault_id, user.organization_id)
        await session.delete(current)


credential_service = CredentialService()

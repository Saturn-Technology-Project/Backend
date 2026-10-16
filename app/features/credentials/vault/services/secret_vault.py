from __future__ import annotations

import asyncio
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.ids import utcnow
from app.features.credentials.vault.models.credential import OrgCredential
from app.features.credentials.vault.models.secret_vault import SecretVault
from app.features.credentials.vault.services.pq_crypto import open_secret, seal
from app.features.mcp.oauth.models.mcp_oauth import McpOAuthPending

_SECRET_ID = re.compile(r"[^A-Za-z0-9_-]+")


def _project() -> str:
    return (get_settings().saturn_secret_project or "").strip()


def _gcp_name(secret_id: str) -> str:
    cleaned = _SECRET_ID.sub("-", secret_id).strip("-")
    return f"sat-{cleaned}"[:255]


async def put_secret(session: AsyncSession, secret_id: str, plaintext: str) -> None:
    if not secret_id:
        return
    if not plaintext:
        await delete_secret(session, secret_id)
        return
    blob = seal(plaintext)
    project = _project()
    if project:
        await asyncio.to_thread(_gcp_put, project, secret_id, blob)
        await _db_delete(session, secret_id)
        return
    row = await session.get(SecretVault, secret_id)
    if row is None:
        session.add(SecretVault(id=secret_id, ciphertext=blob, updated_at=utcnow()))
    else:
        row.ciphertext = blob
        row.updated_at = utcnow()
    await session.flush()


async def get_secret(session: AsyncSession, secret_id: str) -> str:
    if not secret_id:
        return ""
    project = _project()
    if project:
        blob = await asyncio.to_thread(_gcp_get, project, secret_id)
    else:
        row = await session.get(SecretVault, secret_id)
        blob = row.ciphertext if row is not None else ""
    return open_secret(blob)


async def drop_unreadable_secrets(session: AsyncSession) -> None:
    removed: set[str] = set()
    for row in list(await session.scalars(select(SecretVault))):
        if row.ciphertext and open_secret(row.ciphertext):
            continue
        removed.add(row.id)
        await session.delete(row)
    if removed:
        linked = await session.scalars(select(OrgCredential).where(OrgCredential.vault_id.in_(removed)))
        for credential in linked:
            credential.vault_id = ""
    for credential in await session.scalars(select(OrgCredential)):
        if credential.secret_ciphertext and not open_secret(credential.secret_ciphertext):
            credential.secret_ciphertext = ""
    for pending in list(await session.scalars(select(McpOAuthPending))):
        if pending.verifier_ciphertext and not open_secret(pending.verifier_ciphertext):
            await session.delete(pending)
            continue
        if pending.client_secret_ciphertext and not open_secret(pending.client_secret_ciphertext):
            pending.client_secret_ciphertext = ""
    await session.flush()


async def delete_secret(session: AsyncSession, secret_id: str) -> None:
    if not secret_id:
        return
    project = _project()
    if project:
        await asyncio.to_thread(_gcp_delete, project, secret_id)
    await _db_delete(session, secret_id)


async def stored_ids(session: AsyncSession, secret_ids: list[str]) -> set[str]:
    if not secret_ids:
        return set()
    project = _project()
    if project:
        found = set()
        for secret_id in secret_ids:
            if await asyncio.to_thread(_gcp_get, project, secret_id):
                found.add(secret_id)
        return found
    result = await session.scalars(select(SecretVault.id).where(SecretVault.id.in_(secret_ids)))
    return set(result.all())


async def _db_delete(session: AsyncSession, secret_id: str) -> None:
    row = await session.get(SecretVault, secret_id)
    if row is not None:
        await session.delete(row)
        await session.flush()


def _client():
    from google.cloud import secretmanager

    return secretmanager.SecretManagerServiceClient()


def _gcp_put(project: str, secret_id: str, blob: str) -> None:
    from google.api_core.exceptions import NotFound

    client = _client()
    parent = f"projects/{project}"
    name = f"{parent}/secrets/{_gcp_name(secret_id)}"
    try:
        client.get_secret(name=name)
    except NotFound:
        client.create_secret(
            parent=parent,
            secret_id=_gcp_name(secret_id),
            secret={"replication": {"automatic": {}}},
        )
    client.add_secret_version(parent=name, payload={"data": blob.encode()})


def _gcp_get(project: str, secret_id: str) -> str:
    from google.api_core.exceptions import NotFound

    client = _client()
    name = f"projects/{project}/secrets/{_gcp_name(secret_id)}/versions/latest"
    try:
        response = client.access_secret_version(name=name)
    except NotFound:
        return ""
    return response.payload.data.decode()


def _gcp_delete(project: str, secret_id: str) -> None:
    from google.api_core.exceptions import NotFound

    client = _client()
    name = f"projects/{project}/secrets/{_gcp_name(secret_id)}"
    try:
        client.delete_secret(name=name)
    except NotFound:
        return

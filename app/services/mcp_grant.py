from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import McpServer
from app.services.credential_service import open_secret
from app.services.secret_vault import delete_secret, get_secret, put_secret


def grant_id(server_id: str) -> str:
    return f"mcp-{server_id}"


def _empty() -> dict[str, str]:
    return {"access_token": "", "refresh_token": "", "client_secret": ""}


async def read_grant(session: AsyncSession, server: McpServer) -> dict[str, str]:
    if server.credential_id:
        raw = await get_secret(session, server.credential_id)
        parsed = _parse(raw)
        if any(parsed.values()):
            return parsed
    legacy = _empty()
    legacy["access_token"] = open_secret(server.auth_ciphertext)
    legacy["refresh_token"] = open_secret(server.refresh_ciphertext)
    legacy["client_secret"] = open_secret(server.oauth_client_secret)
    if any(legacy.values()):
        await write_grant(session, server, legacy)
    return legacy


async def write_grant(session: AsyncSession, server: McpServer, grant: dict[str, str]) -> None:
    secret_id = grant_id(server.id)
    body = _empty()
    body.update({key: str(grant.get(key) or "") for key in body})
    server.auth_ciphertext = ""
    server.refresh_ciphertext = ""
    server.oauth_client_secret = ""
    if not any(body.values()):
        await delete_secret(session, secret_id)
        server.credential_id = ""
        return
    await put_secret(session, secret_id, json.dumps(body))
    server.credential_id = secret_id


def _parse(raw: str) -> dict[str, str]:
    if not raw:
        return _empty()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"access_token": raw, "refresh_token": "", "client_secret": ""}
    if not isinstance(data, dict):
        return _empty()
    return {
        "access_token": str(data.get("access_token") or ""),
        "refresh_token": str(data.get("refresh_token") or ""),
        "client_secret": str(data.get("client_secret") or ""),
    }

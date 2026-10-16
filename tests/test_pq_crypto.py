import base64

import pytest

from app.features.credentials.vault.services.credential_service import open_secret, seal


def test_credentials_use_mlkem_1024_and_hide_the_plaintext():
    token = seal("gho-example-token")
    assert token.startswith("pq1.")
    assert "gho-example-token" not in token
    assert open_secret(token) == "gho-example-token"
    raw = base64.b64decode(token.removeprefix("pq1."))
    assert len(raw) > 1568


def test_tampered_ciphertext_does_not_open():
    token = seal("gho-example-token")
    raw = bytearray(base64.b64decode(token.removeprefix("pq1.")))
    raw[-1] ^= 1
    broken = "pq1." + base64.b64encode(bytes(raw)).decode()
    assert open_secret(broken) == ""


def test_gateway_seed_is_the_fixed_key():
    from app.features.credentials.vault.services import pq_crypto

    assert len(pq_crypto._FALLBACK_SEED) == 64
    assert pq_crypto._seed() == pq_crypto._FALLBACK_SEED


@pytest.mark.asyncio
async def test_unreadable_vault_ciphertext_is_removed(client):
    from app.core.ids import utcnow
    from app.db.database import get_session
    from app.features.credentials.vault.models.secret_vault import SecretVault
    from app.features.credentials.vault.services.secret_vault import drop_unreadable_secrets
    from app.main import app

    session_gen = app.dependency_overrides[get_session]()
    session = await session_gen.__anext__()
    try:
        session.add(SecretVault(id="legacy", ciphertext="pq1.not-this-key", updated_at=utcnow()))
        session.add(SecretVault(id="current", ciphertext=seal("still-here"), updated_at=utcnow()))
        await session.flush()
        await drop_unreadable_secrets(session)
        assert await session.get(SecretVault, "legacy") is None
        kept = await session.get(SecretVault, "current")
        assert kept is not None
        assert open_secret(kept.ciphertext) == "still-here"
    finally:
        await session_gen.aclose()


@pytest.mark.asyncio
async def test_connected_mcp_keeps_the_token_out_of_the_server_row(client, monkeypatch):
    async def listed(endpoint, authorization=""):
        assert authorization == "access-1"
        return [{"name": "docs", "description": "Docs", "inputSchema": {"type": "object", "properties": {}}}]

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    created = await client.post(
        "/mcp-servers",
        json={"name": "Context7", "endpoint": "https://mcp.context7.com/mcp", "authorization": "access-1"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["has_authorization"] is True
    assert "access-1" not in created.text

    from app.db.database import get_session
    from app.main import app
    from app.models import McpServer, SecretVault

    session_gen = app.dependency_overrides[get_session]()
    session = await session_gen.__anext__()
    try:
        server = await session.get(McpServer, body["id"])
        assert server.credential_id.startswith("mcp-")
        vault = await session.get(SecretVault, server.credential_id)
        assert vault.ciphertext.startswith("pq1.")
        assert "access-1" not in vault.ciphertext
    finally:
        await session_gen.aclose()

import base64

import pytest

from app.services.credential_service import _legacy_seal, open_secret, seal
from app.services.pq_crypto import reset_key


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


def test_legacy_ciphertext_still_opens():
    legacy = _legacy_seal("older-token")
    assert not legacy.startswith("pq1.")
    assert open_secret(legacy) == "older-token"


def test_a_different_kem_seed_cannot_open_the_ciphertext(monkeypatch):
    token = seal("gho-example-token")
    monkeypatch.setenv("SATURN_KEM_SEED", base64.b64encode(b"\x07" * 64).decode())
    reset_key()
    try:
        assert open_secret(token) == ""
    finally:
        monkeypatch.setenv("SATURN_KEM_SEED", base64.b64encode(b"\x42" * 64).decode())
        reset_key()


@pytest.mark.asyncio
async def test_connected_mcp_keeps_the_token_out_of_the_server_row(client, monkeypatch):
    async def listed(endpoint, authorization=""):
        assert authorization == "access-1"
        return [{"name": "docs", "description": "Docs", "inputSchema": {"type": "object", "properties": {}}}]

    monkeypatch.setattr("app.services.mcp_connect.list_remote_tools", listed)
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
        assert server.auth_ciphertext == ""
        assert server.refresh_ciphertext == ""
        assert server.credential_id.startswith("mcp-")
        vault = await session.get(SecretVault, server.credential_id)
        assert vault.ciphertext.startswith("pq1.")
        assert "access-1" not in vault.ciphertext
    finally:
        await session_gen.aclose()

import base64

import pytest

from app.features.credentials.vault.services.credential_service import open_secret, seal


def test_credentials_use_mlkem_1024_and_hide_the_plaintext():
    token = seal("gho-example-token", "org_acme")
    assert token.startswith("pq2.")
    assert "gho-example-token" not in token
    assert open_secret(token, "org_acme") == "gho-example-token"
    assert open_secret(token, "org_other") == ""
    assert open_secret(token, "org_acme", "user_other") == ""
    raw = base64.b64decode(token.removeprefix("pq2."))
    assert len(raw) > 1568


def test_tampered_ciphertext_does_not_open():
    token = seal("gho-example-token", "org_acme")
    raw = bytearray(base64.b64decode(token.removeprefix("pq2.")))
    raw[-1] ^= 1
    broken = "pq2." + base64.b64encode(bytes(raw)).decode()
    assert open_secret(broken, "org_acme") == ""


def test_content_key_is_reused_until_it_expires():
    from app.features.credentials.vault.services import pq_crypto

    pq_crypto._content_keys.clear()
    first = pq_crypto.content_key("org_acme")
    assert pq_crypto.content_key("org_acme") == first
    assert "org_acme" in pq_crypto._content_keys


def test_tenants_do_not_share_a_credential_key():
    from app.features.credentials.vault.services import pq_crypto

    assert pq_crypto.mlkem_seed("org_acme") != pq_crypto.mlkem_seed("org_other")
    assert pq_crypto.mlkem_seed("org_acme", "user_a") != pq_crypto.mlkem_seed("org_acme", "user_b")
    assert pq_crypto.content_key("org_acme") != pq_crypto.content_key("org_other")


def test_missing_kms_key_refuses_to_start(monkeypatch):
    from app.features.credentials.vault.services import pq_crypto

    monkeypatch.setattr(pq_crypto, "_root", None)
    monkeypatch.setattr(pq_crypto, "_keys", {})
    monkeypatch.delenv("SATURN_KMS_KEY", raising=False)
    try:
        with pytest.raises(RuntimeError, match="SATURN_KMS_KEY"):
            pq_crypto.require_root()
    finally:
        pq_crypto.install_root(b"\x11" * 32)


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
        assert vault.ciphertext.startswith("pq2.")
        assert "access-1" not in vault.ciphertext
    finally:
        await session_gen.aclose()

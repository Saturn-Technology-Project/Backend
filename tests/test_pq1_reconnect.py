import pytest

from app.features.credentials.vault.services.pq_crypto import SecretNeedsReconnect, open_secret


def test_pq1_ciphertext_asks_for_reconnect_and_does_not_return_empty():
    with pytest.raises(SecretNeedsReconnect):
        opened = open_secret("pq1.leftover", "org_acme")
        assert opened != ""


async def test_connection_list_reports_pq1_as_needs_reconnect(client):
    from app.core.ids import utcnow
    from app.db.database import get_session
    from app.main import app
    from app.models import McpServer, SecretVault

    override = app.dependency_overrides[get_session]
    generator = override()
    session = await anext(generator)
    try:
        server = await session.get(McpServer, "mcp_email")
        server.credential_id = "vault-pq1-email"
        session.add(SecretVault(id="vault-pq1-email", ciphertext="pq1.leftover", updated_at=utcnow()))
        await session.commit()
    finally:
        await generator.aclose()

    listed = await client.get("/mcp-servers")
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json() if item["id"] == "mcp_email")
    assert row["secret_status"] == "needs_reconnect"
    assert "pq1.leftover" not in listed.text


async def test_pq1_command_lists_and_deletes_without_running_itself(client):
    from app.cli.pq1 import delete_pq1, main, pq1_locations
    from app.core.ids import utcnow
    from app.db.database import get_session
    from app.main import app
    from app.models import SecretVault

    override = app.dependency_overrides[get_session]
    generator = override()
    session = await anext(generator)
    try:
        session.add(SecretVault(id="vault-pq1-only", ciphertext="pq1.leftover", updated_at=utcnow()))
        session.add(SecretVault(id="vault-pq2-keep", ciphertext="pq2.current", updated_at=utcnow()))
        await session.commit()
        found = await pq1_locations(session)
        assert ("secret_vault", "vault-pq1-only") in found
        assert all(row_id != "vault-pq2-keep" for _, row_id in found)
        removed = await delete_pq1(session)
        await session.commit()
        assert ("secret_vault", "vault-pq1-only") in removed
        assert await session.get(SecretVault, "vault-pq1-only") is None
        assert await session.get(SecretVault, "vault-pq2-keep") is not None
    finally:
        await generator.aclose()

    assert main([]) == 2

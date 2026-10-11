import pytest


@pytest.mark.asyncio
async def test_org_secrets_stay_on_the_server(client):
    saved = await client.post(
        "/org/credentials",
        json={
            "kind": "key",
            "name": "Billing",
            "secret_name": "BILLING_API_KEY",
            "secret": "super-secret-value",
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["has_secret"] is True
    assert "secret" not in body
    assert "super-secret-value" not in saved.text

    listed = await client.get("/org/credentials")
    assert listed.status_code == 200
    assert "super-secret-value" not in listed.text
    assert any(item["id"] == body["id"] and item["has_secret"] for item in listed.json())

    hidden = await client.get("/org/credentials/secrets")
    assert hidden.status_code == 403
    assert "super-secret-value" not in hidden.text

    invited = await client.post(
        "/org/invites",
        json={
            "email": "viewer@saturn.ai",
            "name": "Viewer",
            "role": "viewer",
            "agent_ids": ["agent_customer_ops"],
            "mcp_server_ids": ["mcp_crm"],
        },
    )
    assert invited.status_code == 201, invited.text
    accepted = await client.post(
        "/auth/accept-invite",
        json={"token": invited.json()["token"], "password": "password1"},
    )
    assert accepted.status_code == 200, accepted.text
    headers = {"Authorization": f"Bearer {accepted.json()['token']}"}

    denied_read = await client.get("/org/credentials/secrets", headers=headers)
    assert denied_read.status_code == 403
    denied_write = await client.post(
        "/org/credentials",
        headers=headers,
        json={"kind": "key", "name": "Other", "secret": "another-secret"},
    )
    assert denied_write.status_code == 403
    denied_delete = await client.delete(f"/org/credentials/{body['id']}", headers=headers)
    assert denied_delete.status_code == 403
    assert "super-secret-value" not in denied_read.text
    assert "another-secret" not in denied_write.text

    removed = await client.delete(f"/org/credentials/{body['id']}")
    assert removed.status_code == 204

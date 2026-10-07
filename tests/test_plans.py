import pytest


@pytest.mark.asyncio
async def test_free_is_personal_and_pro_can_use_saturn_mcp(client):
    created = await client.post(
        "/admin/orgs",
        json={
            "name": "Ada",
            "admin_email": "ada@personal.test",
            "admin_name": "Ada",
            "admin_password": "password1",
            "plan": "free",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["kind"] == "personal"

    signed = await client.post("/auth/login", json={"email": "ada@personal.test", "password": "password1"})
    assert signed.status_code == 200, signed.text
    token = signed.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    blocked = await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "Saturn MCP requires Pro"

    invited = await client.post(
        "/org/invites",
        headers=headers,
        json={"email": "bea@personal.test", "name": "Bea"},
    )
    assert invited.status_code == 403

    upgraded = await client.patch(f"/admin/orgs/{created.json()['id']}", json={"plan": "pro"})
    assert upgraded.status_code == 200
    assert upgraded.json()["kind"] == "personal"

    allowed = await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert allowed.status_code == 200
    still_personal = await client.post(
        "/org/invites",
        headers=headers,
        json={"email": "bea@personal.test", "name": "Bea"},
    )
    assert still_personal.status_code == 403

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
    assert signed.json()["user"]["profile_id"] == signed.json()["user"]["id"]
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
    mismatched = await client.post(
        "/mcp",
        headers={**headers, "X-Saturn-Profile": "org_other:someone"},
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}},
    )
    assert mismatched.status_code == 200
    assert mismatched.json()["error"]["message"] == "Profile does not match"
    matched = await client.post(
        "/mcp",
        headers={**headers, "X-Saturn-Profile": signed.json()["user"]["id"]},
        json={"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}},
    )
    assert matched.status_code == 200
    assert matched.json()["error"]["message"] == "Unknown agent"
    created_agent = await client.post("/agents", headers=headers, json={"name": "Ada agent"})
    assert created_agent.status_code == 201, created_agent.text
    agent_id = created_agent.json()["id"]
    bound = await client.post(
        "/mcp",
        headers={
            **headers,
            "X-Saturn-Profile": signed.json()["user"]["id"],
            "X-Saturn-Agent-Id": agent_id,
        },
        json={"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {}},
    )
    assert bound.status_code == 200
    listed = await client.get(f"/agents/{agent_id}", headers=headers)
    assert listed.json()["profile_id"] == signed.json()["user"]["id"]
    assert not listed.json().get("origin")
    opened = await client.post(
        "/mcp",
        headers={**headers, "X-Saturn-Profile": signed.json()["user"]["id"]},
        json={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "Cursor", "version": "1"},
            },
        },
    )
    assert opened.status_code == 200, opened.text
    visible = await client.get("/agents", headers=headers)
    cursor = next(item for item in visible.json() if item["name"] == "Cursor")
    assert cursor["origin"] == "saturn_mcp"
    assert cursor["profile_id"] == signed.json()["user"]["id"]
    account = await client.get("/auth/me", headers=headers)
    assert all(item["name"] != "Cursor" for item in account.json()["agents"])
    still_personal = await client.post(
        "/org/invites",
        headers=headers,
        json={"email": "bea@personal.test", "name": "Bea"},
    )
    assert still_personal.status_code == 403


@pytest.mark.asyncio
async def test_public_register_creates_a_personal_org(client):
    created = await client.post(
        "/auth/register",
        json={"name": "Mina", "email": "mina@personal.test", "password": "password1"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["token"]
    assert body["user"]["organization"]["kind"] == "personal"
    assert body["user"]["organization"]["plan"] == "free"
    assert body["user"]["organization"]["name"] == "Mina"
    assert body["user"]["profile_id"] == body["user"]["id"]

    signed = await client.post(
        "/auth/login",
        json={"email": "mina@personal.test", "password": "password1"},
    )
    assert signed.status_code == 200, signed.text
    headers = {"Authorization": f"Bearer {signed.json()['token']}"}
    invited = await client.post(
        "/org/invites",
        headers=headers,
        json={"email": "other@personal.test", "name": "Other"},
    )
    assert invited.status_code == 403

    duplicate = await client.post(
        "/auth/register",
        json={"name": "Mina", "email": "mina@personal.test", "password": "password1"},
    )
    assert duplicate.status_code == 409

    listed = (await client.get("/admin/orgs")).json()
    org = next(item for item in listed if item["name"] == "Mina")
    assert org["kind"] == "personal"
    assert org["plan"] == "free"
    assert org["runtime_status"] == "app"
    detail = await client.get(f"/admin/orgs/{org['id']}")
    assert "Connector not connected" not in detail.json()["health"]["issues"]

    team = await client.post(
        "/admin/orgs",
        json={
            "name": "Team Co",
            "admin_email": "lead@team.test",
            "admin_name": "Lead",
            "admin_password": "password1",
            "plan": "enterprise",
            "kind": "organization",
        },
    )
    assert team.status_code == 201, team.text
    assert team.json()["kind"] == "organization"

    mismatch = await client.post(
        "/admin/orgs",
        json={
            "name": "Nope",
            "admin_email": "nope@team.test",
            "admin_name": "Nope",
            "admin_password": "password1",
            "plan": "enterprise",
            "kind": "personal",
        },
    )
    assert mismatch.status_code == 400

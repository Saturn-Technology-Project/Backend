async def test_app_mcp_syncs_separately_from_cloud_connections(client):
    saved = await client.put(
        "/app-mcp",
        json={
            "servers": [
                {
                    "id": "mcp_local",
                    "name": "Mine",
                    "url": "http://127.0.0.1:9/mine",
                    "tools": [{"tool_id": "mine.search", "description": "Search"}],
                },
                {"id": "saturn:agent_cloud", "name": "Saturn MCP", "url": "https://example.com/mcp", "tools": []},
            ],
            "attachments": {"agent_customer_ops": ["mcp_local", "saturn:agent_cloud"]},
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert [item["id"] for item in body["servers"]] == ["mcp_local"]
    assert body["attachments"] == {"agent_customer_ops": ["mcp_local"]}

    listed = (await client.get("/app-mcp")).json()
    assert listed["servers"][0]["name"] == "Mine"
    connections = (await client.get("/mcp-servers")).json()
    assert all(item["id"] != "mcp_local" for item in connections)

    me = (await client.get("/auth/me")).json()
    assert me["app_mcp"]["attachments"]["agent_customer_ops"] == ["mcp_local"]
    assert all(item["id"] != "mcp_local" for item in me["mcp_servers"])


async def test_team_mcp_is_shared_and_stays_off_personal_mcp(client):
    invited = await client.post(
        "/org/invites",
        json={"email": "viewer@saturn.ai", "name": "Viewer", "title": "Viewer", "role": "viewer"},
    )
    assert invited.status_code == 201, invited.text
    accepted = await client.post(
        "/auth/accept-invite",
        json={"token": invited.json()["token"], "password": "password1"},
    )
    assert accepted.status_code == 200, accepted.text
    viewer = {"Authorization": f"Bearer {accepted.json()['token']}"}

    saved = await client.put(
        "/org-app-mcp",
        json={
            "servers": [
                {
                    "id": "mcp_team",
                    "name": "Ops",
                    "url": "http://127.0.0.1:9/ops",
                    "tools": [{"tool_id": "ops.search", "description": "Search"}],
                }
            ],
            "attachments": {"agent_ops": ["mcp_team"], "saturn-agent": ["mcp_team"]},
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["attachments"] == {"agent_ops": ["mcp_team"]}

    seen = await client.get("/org-app-mcp", headers=viewer)
    assert seen.status_code == 200, seen.text
    assert seen.json()["servers"][0]["name"] == "Ops"
    denied = await client.put(
        "/org-app-mcp",
        headers=viewer,
        json={"servers": [{"id": "mcp_no", "name": "Nope", "url": "http://127.0.0.1:9/no"}], "attachments": {}},
    )
    assert denied.status_code == 403
    personal = await client.get("/app-mcp", headers=viewer)
    assert personal.json()["servers"] == []

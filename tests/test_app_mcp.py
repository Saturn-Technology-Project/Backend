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

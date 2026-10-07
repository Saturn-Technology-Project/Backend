async def test_mapped_tool_set_is_what_saturn_mcp_exposes(client, monkeypatch):
    async def listed(endpoint, authorization=""):
        assert endpoint == "https://github.example/mcp"
        assert authorization == "token-1"
        return [
            {"name": "repo.read", "description": "Read a repo", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "repo.delete", "description": "Delete a repo", "inputSchema": {"type": "object", "properties": {}}},
        ]

    async def called(endpoint, name, arguments, authorization=""):
        assert endpoint == "https://github.example/mcp"
        assert authorization == "token-1"
        assert name in {"repo.read", "repo.delete"}
        return {"content": [{"type": "text", "text": "ok"}], "tool": name, "echo": arguments}

    monkeypatch.setattr("app.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.services.saturn_mcp.call_remote_tool", called)

    connected = await client.post(
        "/mcp-servers",
        json={"name": "GitHub", "endpoint": "https://github.example/mcp", "authorization": "token-1"},
    )
    assert connected.status_code == 201, connected.text
    body = connected.json()
    assert body["has_authorization"] is True
    assert "auth" not in body
    catalog = {item["name"]: item["id"] for item in body["catalog"]}
    assert set(catalog) == {"repo.read", "repo.delete"}

    created = await client.post(
        "/tool-sets",
        json={
            "name": "Coding Agent MCP",
            "items": [
                {"tool_id": catalog["repo.read"], "approval": False},
                {"tool_id": catalog["repo.delete"], "approval": True},
            ],
        },
    )
    assert created.status_code == 201, created.text
    tool_set_id = created.json()["id"]

    assigned = await client.patch(
        "/agents/agent_customer_ops",
        json={"tool_set_id": tool_set_id},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["tool_set_id"] == tool_set_id

    headers = {"X-Saturn-Agent-Id": "agent_customer_ops"}
    listed_tools = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, headers=headers)
    assert listed_tools.status_code == 200, listed_tools.text
    names = [item["name"] for item in listed_tools.json()["result"]["tools"]]
    assert names == ["repo.delete", "repo.read"]

    allowed = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "repo.read", "arguments": {"repo": "saturn"}}},
        headers={**headers, "X-Saturn-Run-Id": "run_map_1", "X-Saturn-Action-Id": "act_map_read"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["result"]["echo"] == {"repo": "saturn"}

    held = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "repo.delete", "arguments": {"repo": "saturn"}}},
        headers={**headers, "X-Saturn-Run-Id": "run_map_2", "X-Saturn-Action-Id": "act_map_delete"},
    )
    assert held.status_code == 200, held.text
    error = held.json()["error"]
    assert error["message"] == "approval_required"
    approval_id = error["data"]["approval_id"]

    decision = await client.post(f"/approvals/{approval_id}/approve")
    assert decision.status_code == 200, decision.text

    resumed = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "repo.delete", "arguments": {"repo": "saturn"}}},
        headers={**headers, "X-Saturn-Run-Id": "run_map_2", "X-Saturn-Action-Id": "act_map_delete"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["result"]["tool"] == "repo.delete"

    hidden = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "repo.missing", "arguments": {}}},
        headers=headers,
    )
    assert hidden.json()["error"]["message"] == "Agent is not allowed to call this tool"

    other = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}},
        headers={"X-Saturn-Agent-Id": "agent_unmapped"},
    )
    assert other.json()["result"]["tools"] == []

    denied = await client.post(
        "/policies",
        json={
            "name": "No deletes",
            "agent_ids": ["agent_customer_ops"],
            "tool": "repo.read",
            "rules": [{"tool": "repo.read", "condition": "true", "decision": "deny"}],
        },
    )
    assert denied.status_code == 201, denied.text
    blocked = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "repo.read", "arguments": {"repo": "saturn"}}},
        headers={**headers, "X-Saturn-Run-Id": "run_map_3", "X-Saturn-Action-Id": "act_map_denied"},
    )
    assert blocked.json()["error"]["message"] == "policy_denied"

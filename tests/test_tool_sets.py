from tests.oauth_access import oauth_access


async def test_each_agent_sees_only_its_own_tool_set(client, monkeypatch):
    async def listed(endpoint, authorization=""):
        return [
            {"name": "repo.read", "description": "Read", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "repo.delete", "description": "Delete", "inputSchema": {"type": "object", "properties": {}}},
        ]

    async def called(endpoint, name, arguments, authorization=""):
        return {"content": [{"type": "text", "text": "ok"}], "tool": name}

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.saturn.services.saturn_mcp.call_remote_tool", called)

    connected = await client.post(
        "/mcp-servers",
        json={"name": "GitHub", "endpoint": "https://api.githubcopilot.com/mcp", "authorization": "token-1"},
    )
    assert connected.status_code == 201, connected.text
    read_id = next(item["id"] for item in connected.json()["catalog"] if item["name"] == "repo.read")

    cursor_token = await oauth_access(client, "Cursor")
    claude_token = await oauth_access(client, "Claude")
    for access in (cursor_token, claude_token):
        opened = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {access}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert opened.status_code == 200, opened.text

    agents = {item["name"]: item for item in (await client.get("/agents")).json()}
    created = await client.post(
        "/tool-sets",
        json={"name": "Coding", "items": [{"tool_id": read_id, "approval": False}]},
    )
    assert created.status_code == 201, created.text
    tool_set_id = created.json()["id"]
    assigned = await client.patch(f"/agents/{agents['Cursor']['id']}", json={"tool_set_id": tool_set_id})
    assert assigned.status_code == 200, assigned.text

    async def names(access: str) -> list[str]:
        listed_tools = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {access}"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert listed_tools.status_code == 200, listed_tools.text
        body = listed_tools.json()
        assert "error" not in body, body
        return [item["name"] for item in body["result"]["tools"]]

    assert await names(cursor_token) == ["repo.read"]
    assert await names(claude_token) == []

    hidden = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {cursor_token}"},
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "repo.delete", "arguments": {}}},
    )
    assert hidden.json()["error"]["message"] == "Agent is not allowed to call this tool"
    allowed = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {cursor_token}"},
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "repo.read", "arguments": {"repo": "saturn"}}},
    )
    assert allowed.json()["result"]["tool"] == "repo.read"

    moved = await client.patch(f"/agents/{agents['Claude']['id']}", json={"tool_set_id": tool_set_id})
    assert moved.status_code == 200, moved.text
    cleared = await client.patch(f"/agents/{agents['Cursor']['id']}", json={"tool_set_id": None})
    assert cleared.status_code == 200, cleared.text
    assert await names(cursor_token) == []
    assert await names(claude_token) == ["repo.read"]


async def test_mapped_tool_set_is_what_saturn_mcp_exposes(client, monkeypatch):
    async def listed(endpoint, authorization=""):
        assert endpoint == "https://api.githubcopilot.com/mcp"
        assert authorization == "token-1"
        return [
            {"name": "repo.read", "description": "Read a repo", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "repo.delete", "description": "Delete a repo", "inputSchema": {"type": "object", "properties": {}}},
        ]

    async def called(endpoint, name, arguments, authorization=""):
        assert endpoint == "https://api.githubcopilot.com/mcp"
        assert authorization == "token-1"
        assert name in {"repo.read", "repo.delete"}
        return {"content": [{"type": "text", "text": "ok"}], "tool": name, "echo": arguments}

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.saturn.services.saturn_mcp.call_remote_tool", called)

    connected = await client.post(
        "/mcp-servers",
        json={"name": "GitHub", "endpoint": "https://api.githubcopilot.com/mcp", "authorization": "token-1"},
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

    access = await oauth_access(client, "Mapped Agent")
    opened = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert opened.status_code == 200, opened.text
    agent_id = next(item["id"] for item in (await client.get("/agents")).json() if item["name"] == "Mapped Agent")
    assigned = await client.patch(f"/agents/{agent_id}", json={"tool_set_id": tool_set_id})
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["tool_set_id"] == tool_set_id

    session_call = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"X-Saturn-Agent-Id": agent_id},
    )
    assert session_call.status_code == 401

    headers = {"Authorization": f"Bearer {access}", "X-Saturn-Agent-Id": agent_id}
    listed_tools = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, headers=headers)
    assert listed_tools.status_code == 200, listed_tools.text
    names = [item["name"] for item in listed_tools.json()["result"]["tools"]]
    assert names == ["repo.delete", "repo.read"]
    delete_tool = next(item for item in listed_tools.json()["result"]["tools"] if item["name"] == "repo.delete")
    assert "saturnRunId" in delete_tool["inputSchema"]["properties"]

    allowed = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "repo.read", "arguments": {"repo": "saturn"}}},
        headers=headers,
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["result"]["echo"] == {"repo": "saturn"}

    unlinked = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "repo.read", "arguments": {"repo": "docs"}}},
        headers=headers,
    )
    assert unlinked.status_code == 200, unlinked.text
    assert unlinked.json()["result"]["echo"] == {"repo": "docs"}

    held = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "repo.delete", "arguments": {"repo": "saturn"}}},
        headers=headers,
    )
    assert held.status_code == 200, held.text
    error = held.json()["error"]
    assert error["message"].startswith("approval_required")
    assert "saturnRunId" in error["message"]
    approval_id = error["data"]["approval_id"]
    run_id = error["data"]["saturnRunId"]

    decision = await client.post(f"/approvals/{approval_id}/approve")
    assert decision.status_code == 200, decision.text

    resumed = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "repo.delete", "arguments": {"repo": "saturn", "saturnRunId": run_id}},
        },
        headers=headers,
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["result"]["tool"] == "repo.delete"

    hidden = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "repo.missing", "arguments": {}}},
        headers=headers,
    )
    assert hidden.json()["error"]["message"] == "Agent is not allowed to call this tool"
    activity = (await client.get("/activity")).json()
    denied = next(item for item in activity if item["tool"] == "repo.missing")
    assert denied["event"] == "tool_requested"
    assert denied["result"] == "denied"
    assert denied["actor_id"] == agent_id
    assert denied["payload"]["code"] == -32003
    assert denied["payload"]["reason"] == "Agent is not allowed to call this tool"

    other = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}},
        headers={"Authorization": f"Bearer {access}", "X-Saturn-Agent-Id": "agent_unmapped"},
    )
    assert other.json()["error"]["message"] == "Unknown agent"

    stranger = await oauth_access(client, "Stranger")
    stolen = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}},
        headers={"Authorization": f"Bearer {stranger}", "X-Saturn-Agent-Id": agent_id},
    )
    assert stolen.json()["error"]["message"] == "Unknown agent"

    denied = await client.post(
        "/policies",
        json={
            "name": "No deletes",
            "agent_ids": [agent_id],
            "tool": "repo.read",
            "rules": [{"tool": "repo.read", "condition": "true", "decision": "deny"}],
        },
    )
    assert denied.status_code == 201, denied.text
    blocked = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "repo.read", "arguments": {"repo": "saturn"}}},
        headers=headers,
    )
    assert blocked.json()["error"]["message"] == "policy_denied"


async def test_agent_resubmits_by_saturn_run_id_argument(client, monkeypatch):
    seen = {}

    async def listed(endpoint, authorization=""):
        return [{"name": "repo.delete", "description": "Delete", "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}}}}]

    async def called(endpoint, name, arguments, authorization=""):
        seen["arguments"] = arguments
        return {"content": [{"type": "text", "text": "ok"}], "echo": arguments}

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.saturn.services.saturn_mcp.call_remote_tool", called)
    connected = await client.post(
        "/mcp-servers",
        json={"name": "GitHub", "endpoint": "https://api.githubcopilot.com/mcp", "authorization": "token-1"},
    )
    tool_id = next(item["id"] for item in connected.json()["catalog"] if item["name"] == "repo.delete")
    created = await client.post("/tool-sets", json={"name": "Deletes", "items": [{"tool_id": tool_id, "approval": True}]})
    access = await oauth_access(client, "Argument Agent")
    await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    agent_id = next(item["id"] for item in (await client.get("/agents")).json() if item["name"] == "Argument Agent")
    await client.patch(f"/agents/{agent_id}", json={"tool_set_id": created.json()["id"]})
    headers = {"Authorization": f"Bearer {access}"}
    first = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "repo.delete", "arguments": {"repo": "saturn"}}},
        headers=headers,
    )
    error = first.json()["error"]
    assert error["message"].startswith("approval_required")
    run_id = error["data"]["saturnRunId"]
    assert run_id
    assert error["data"]["argument"] == "saturnRunId"
    approval_id = error["data"]["approval_id"]
    again = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "repo.delete", "arguments": {"repo": "saturn", "saturnRunId": run_id}},
        },
        headers=headers,
    )
    assert again.json()["error"]["data"]["approval_id"] == approval_id
    assert (await client.post(f"/approvals/{approval_id}/approve")).status_code == 200
    resumed = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "repo.delete", "arguments": {"repo": "saturn", "saturnRunId": run_id}},
        },
        headers=headers,
    )
    assert resumed.json()["result"]["echo"] == {"repo": "saturn"}
    assert "saturnRunId" not in seen["arguments"]

    rejected = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "repo.delete", "arguments": {"repo": "other"}}},
        headers=headers,
    )
    rejected_error = rejected.json()["error"]
    rejected_id = rejected_error["data"]["approval_id"]
    rejected_run = rejected_error["data"]["saturnRunId"]
    assert (await client.post(f"/approvals/{rejected_id}/reject")).status_code == 200
    repeated = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "repo.delete", "arguments": {"repo": "other", "saturnRunId": rejected_run}},
        },
        headers=headers,
    )
    assert repeated.json()["error"]["message"].startswith("approval_rejected")
    assert repeated.json()["error"]["data"]["approval_id"] == rejected_id

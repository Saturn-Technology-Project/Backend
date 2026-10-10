import re
from urllib.parse import parse_qs, urlparse

from app.services.oauth_service import pkce_s256


async def _oauth_access(client, client_name: str) -> str:
    saved = client.headers.get("Authorization")
    client.headers.pop("Authorization", None)
    verifier = "c" * 43
    challenge = pkce_s256(verifier)
    registered = await client.post(
        "/oauth/register",
        json={"client_name": client_name, "redirect_uris": ["http://127.0.0.1:8765/callback"]},
    )
    assert registered.status_code == 201, registered.text
    query = {
        "response_type": "code",
        "client_id": registered.json()["client_id"],
        "redirect_uri": "http://127.0.0.1:8765/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": client_name,
    }
    await client.get("/oauth/authorize", params=query)
    signed = await client.post(
        "/oauth/login",
        data={**query, "email": "rick@saturn.ai", "password": "saturn"},
        follow_redirects=False,
    )
    assert signed.status_code == 303, signed.text
    consent = await client.get(signed.headers["location"])
    csrf = re.search(r'name="csrf" value="([^"]+)"', consent.text)
    assert csrf is not None
    allowed = await client.post(
        "/oauth/consent",
        data={**query, "csrf": csrf.group(1), "decision": "allow"},
        follow_redirects=False,
    )
    assert allowed.status_code == 303, allowed.text
    code = parse_qs(urlparse(allowed.headers["location"]).query)["code"][0]
    token = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": query["client_id"],
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    if saved:
        client.headers["Authorization"] = saved
    return token.json()["access_token"]


async def test_each_agent_sees_only_its_own_tool_set(client, monkeypatch):
    async def listed(endpoint, authorization=""):
        return [
            {"name": "repo.read", "description": "Read", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "repo.delete", "description": "Delete", "inputSchema": {"type": "object", "properties": {}}},
        ]

    async def called(endpoint, name, arguments, authorization=""):
        return {"content": [{"type": "text", "text": "ok"}], "tool": name}

    monkeypatch.setattr("app.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.services.saturn_mcp.call_remote_tool", called)

    connected = await client.post(
        "/mcp-servers",
        json={"name": "GitHub", "endpoint": "https://github.example/mcp", "authorization": "token-1"},
    )
    assert connected.status_code == 201, connected.text
    read_id = next(item["id"] for item in connected.json()["catalog"] if item["name"] == "repo.read")

    cursor_token = await _oauth_access(client, "Cursor")
    claude_token = await _oauth_access(client, "Claude")
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

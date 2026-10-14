import pytest

from app.features.organization.org.services.plans import allows_pro, allows_saturn_cloud, entitlements
from tests.oauth_access import oauth_access


def test_unknown_plan_keeps_the_gateway_and_denies_pro():
    assert allows_saturn_cloud(None) is True
    assert allows_saturn_cloud("nope") is True
    assert allows_pro(None) is False
    assert allows_pro("") is False
    assert allows_pro("nope") is False
    assert allows_pro("trial") is False
    assert allows_pro("starter") is True
    assert allows_pro("enterprise") is True
    free = entitlements("free")
    assert free["tool_set_limit"] is None
    assert free["policies"] is False
    assert free["webhooks"] is False
    assert free["audit"] == "full"
    assert free["audit_days"] is None
    assert entitlements(None)["policies"] is False
    assert entitlements("enterprise")["tool_set_limit"] is None
    assert entitlements("enterprise")["audit"] == "full"
    assert entitlements("enterprise")["audit_days"] is None


async def _onboard(client, *, name: str, email: str, plan: str) -> str:
    created = await client.post(
        "/admin/orgs",
        json={
            "name": name,
            "admin_email": email,
            "admin_name": name,
            "admin_password": "password1",
            "plan": plan,
        },
    )
    assert created.status_code == 201, created.text
    signed = await client.post("/auth/login", json={"email": email, "password": "password1"})
    assert signed.status_code == 200, signed.text
    return signed.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_seed_org_is_pro(client):
    org = (await client.get("/org")).json()["organization"]
    assert org["plan"] == "enterprise"
    assert org["entitlements"]["policies"] is True
    assert org["entitlements"]["webhooks"] is True
    assert org["entitlements"]["audit"] == "full"
    assert org["entitlements"]["tool_set_limit"] is None


@pytest.mark.asyncio
async def test_free_connects_one_shared_set_and_cannot_use_pro_apis(client, monkeypatch):
    async def listed(endpoint, authorization=""):
        return [
            {"name": "repo.read", "description": "Read", "inputSchema": {"type": "object"}},
            {"name": "repo.delete", "description": "Delete", "inputSchema": {"type": "object"}},
        ]

    async def called(endpoint, name, arguments, authorization=""):
        return {"content": [{"type": "text", "text": "ok"}], "tool": name, "echo": arguments}

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.saturn.services.saturn_mcp.call_remote_tool", called)

    token = await _onboard(client, name="Free", email="free-ent@example.com", plan="free")
    headers = _auth(token)

    connected = await client.post(
        "/mcp-servers",
        headers=headers,
        json={"name": "GitHub", "endpoint": "https://api.githubcopilot.com/free", "authorization": "token-1"},
    )
    assert connected.status_code == 201, connected.text
    catalog = {item["name"]: item["id"] for item in connected.json()["catalog"]}

    created = await client.post(
        "/tool-sets",
        headers=headers,
        json={"name": "Only", "items": [{"tool_id": catalog["repo.read"], "approval": False}]},
    )
    assert created.status_code == 201, created.text
    tool_set_id = created.json()["id"]

    second = await client.post(
        "/tool-sets",
        headers=headers,
        json={"name": "Another", "items": [{"tool_id": catalog["repo.read"], "approval": False}]},
    )
    assert second.status_code == 201, second.text

    updated = await client.patch(
        f"/tool-sets/{tool_set_id}",
        headers=headers,
        json={
            "items": [
                {"tool_id": catalog["repo.read"], "approval": False},
                {"tool_id": catalog["repo.delete"], "approval": True},
            ]
        },
    )
    assert updated.status_code == 200, updated.text
    modes = {item["tool_id"]: item["approval"] for item in updated.json()["items"]}
    assert modes[catalog["repo.read"]] is False
    assert modes[catalog["repo.delete"]] is True

    first = await client.post("/agents", headers=headers, json={"name": "One"})
    second_agent = await client.post("/agents", headers=headers, json={"name": "Two"})
    assert first.status_code == 201 and second_agent.status_code == 201
    for agent_id in (first.json()["id"], second_agent.json()["id"]):
        assigned = await client.patch(f"/agents/{agent_id}", headers=headers, json={"tool_set_id": tool_set_id})
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["tool_set_id"] == tool_set_id

    blocked = await client.post(
        "/mcp",
        headers={**headers, "X-Saturn-Agent-Id": first.json()["id"]},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "repo.read", "arguments": {"repo": "saturn"}}},
    )
    assert blocked.status_code == 401

    access = await oauth_access(client, "Free Caller", email="free-ent@example.com", password="password1")
    opened = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert opened.status_code == 200, opened.text
    caller_id = next(item["id"] for item in (await client.get("/agents", headers=headers)).json() if item["name"] == "Free Caller")
    caller_set = await client.patch(f"/agents/{caller_id}", headers=headers, json={"tool_set_id": tool_set_id})
    assert caller_set.status_code == 200, caller_set.text
    agent_headers = {"Authorization": f"Bearer {access}", "X-Saturn-Agent-Id": caller_id}
    stolen = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access}", "X-Saturn-Agent-Id": first.json()["id"]},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "repo.read", "arguments": {"repo": "saturn"}}},
    )
    assert stolen.json()["error"]["message"] == "Unknown agent"
    allowed = await client.post(
        "/mcp",
        headers=agent_headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "repo.read", "arguments": {"repo": "saturn"}}},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["result"]["echo"] == {"repo": "saturn"}

    held = await client.post(
        "/mcp",
        headers=agent_headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "repo.delete", "arguments": {"repo": "saturn"}}},
    )
    assert held.json()["error"]["message"].startswith("approval_required")

    hidden = await client.post(
        "/mcp",
        headers=agent_headers,
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "repo.missing", "arguments": {}}},
    )
    assert hidden.json()["error"]["message"] == "Agent is not allowed to call this tool"

    assert (await client.get("/policies", headers=headers)).json() == []
    for path, body in (
        ("/policies", {"name": "Hidden", "rules": []}),
        ("/policies/generate", {"tool_set_id": tool_set_id, "name": "Draft"}),
        ("/policies/pol_missing/validate", None),
        ("/policies/pol_missing/simulate", {"tool": "repo.read"}),
        ("/policies/pol_missing/optimize", {"definition": {"nodes": []}}),
    ):
        response = await client.post(path, headers=headers, json=body)
        assert response.status_code == 403, path
        assert response.json()["detail"] == "This feature requires Pro"

    activity = await client.get("/activity", headers=headers)
    assert activity.status_code == 200
    assert activity.json()
    assert all("arguments" not in (item.get("payload") or {}) for item in activity.json())
    assert all(item.get("tool") or item.get("event") for item in activity.json())

    searched = await client.get("/activity", headers=headers, params={"q": "repo"})
    assert searched.status_code == 200
    assert searched.json()
    exported = await client.get("/activity/export", headers=headers)
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-disposition"].startswith("attachment;")

    monkeypatch.setattr(
        "app.features.execution.activity.controllers.activity.audit_retention_days",
        lambda _plan: -1,
    )
    hidden_rows = await client.get("/activity", headers=headers)
    assert hidden_rows.status_code == 200
    assert hidden_rows.json() == []
    monkeypatch.setattr(
        "app.features.execution.activity.controllers.activity.audit_retention_days",
        lambda _plan: None,
    )
    restored = await client.get("/activity", headers=headers)
    assert restored.status_code == 200
    assert restored.json()


@pytest.mark.asyncio
async def test_pro_manages_policies_sets_and_audit(client, monkeypatch):
    async def listed(endpoint, authorization=""):
        return [{"name": "repo.read", "description": "Read", "inputSchema": {"type": "object"}}]

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)

    token = await _onboard(client, name="Pro", email="pro-ent@example.com", plan="pro")
    headers = _auth(token)
    connected = await client.post(
        "/mcp-servers",
        headers=headers,
        json={"name": "GitHub", "endpoint": "https://api.githubcopilot.com/pro", "authorization": "token-1"},
    )
    assert connected.status_code == 201, connected.text
    tool_id = connected.json()["catalog"][0]["id"]

    first = await client.post(
        "/tool-sets",
        headers=headers,
        json={"name": "One", "items": [{"tool_id": tool_id, "approval": False}]},
    )
    second = await client.post(
        "/tool-sets",
        headers=headers,
        json={"name": "Two", "items": [{"tool_id": tool_id, "approval": True}]},
    )
    assert first.status_code == 201 and second.status_code == 201

    created = await client.post(
        "/policies",
        headers=headers,
        json={
            "name": "Reads",
            "tool_set_id": first.json()["id"],
            "rules": [{"tool": "repo.read", "condition": "true", "decision": "allow"}],
        },
    )
    assert created.status_code == 201, created.text
    policy_id = created.json()["id"]
    assert (await client.get("/policies", headers=headers)).status_code == 200
    assert any(item["id"] == policy_id for item in (await client.get("/policies", headers=headers)).json())

    validated = await client.post(f"/policies/{policy_id}/validate", headers=headers)
    assert validated.status_code == 200, validated.text
    simulated = await client.post(
        f"/policies/{policy_id}/simulate",
        headers=headers,
        json={"tool": "repo.read", "arguments": {}},
    )
    assert simulated.status_code == 200, simulated.text
    generated = await client.post(
        "/policies/generate",
        headers=headers,
        json={"tool_set_id": first.json()["id"], "name": "Generated"},
    )
    assert generated.status_code == 201, generated.text
    optimized = await client.post(
        f"/policies/{policy_id}/optimize",
        headers=headers,
        json={"definition": {"nodes": [], "edges": [], "toolSetId": first.json()["id"]}},
    )
    assert optimized.status_code != 403
    if optimized.status_code == 200:
        assert "diff" in optimized.json()
    else:
        assert optimized.status_code == 503

    exported = await client.get("/activity/export", headers=headers)
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-disposition"].startswith("attachment;")
    searched = await client.get("/activity", headers=headers, params={"q": "does-not-match"})
    assert searched.status_code == 200
    assert searched.json() == []


@pytest.mark.asyncio
async def test_plan_does_not_cross_organizations(client):
    free = await _onboard(client, name="Free Isolated", email="free-iso@example.com", plan="free")
    other = await _onboard(client, name="Other Pro", email="other-pro@example.com", plan="pro")
    created = await client.post(
        "/policies",
        headers=_auth(other),
        json={"name": "Private", "rules": [{"tool": "repo.read", "condition": "true", "decision": "deny"}]},
    )
    assert created.status_code == 201, created.text
    stolen = await client.get(f"/policies/{created.json()['id']}", headers=_auth(free))
    assert stolen.status_code == 403
    missing = await client.get(f"/policies/{created.json()['id']}", headers=_auth(other))
    assert missing.status_code == 200
    outsider = await _onboard(client, name="Third", email="third-pro@example.com", plan="pro")
    hidden = await client.get(f"/policies/{created.json()['id']}", headers=_auth(outsider))
    assert hidden.status_code == 404
    listed = await client.get("/tool-sets", headers=_auth(free))
    assert listed.status_code == 200
    assert listed.json() == []


@pytest.mark.asyncio
async def test_rbac_blocks_policy_writes_on_a_pro_org(client):
    invited = await client.post(
        "/org/invites",
        json={
            "email": "viewer-ent@saturn.ai",
            "name": "Viewer",
            "title": "Analyst",
            "role": "viewer",
        },
    )
    assert invited.status_code == 201, invited.text
    accepted = await client.post(
        "/auth/accept-invite",
        json={"token": invited.json()["token"], "password": "password1"},
    )
    assert accepted.status_code == 200, accepted.text
    headers = _auth(accepted.json()["token"])
    denied = await client.post("/policies", headers=headers, json={"name": "No", "rules": []})
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Missing policies.write access"
    assert (await client.get("/policies", headers=headers)).status_code == 200


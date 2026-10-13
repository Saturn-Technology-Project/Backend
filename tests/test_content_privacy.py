from app.features.execution.activity.services.content_seal import (
    open_json,
    plain_arguments,
    policy_fields,
    public_payload,
    seal_arguments,
    seal_json,
    seal_payload,
)
from tests.oauth_access import oauth_access


class _Rule:
    def __init__(self):
        self.conditions = [{"field": "amount", "operator": "gt", "value": 1000}]
        self.expression = None
        self.condition = "amount > 1000"


def test_only_policy_fields_stay_visible():
    sealed = seal_payload(
        "org_test",
        {
            "arguments": {"amount": 5800, "note": "private body"},
            "raw_request": {"method": "tools/call"},
            "policy_fields": policy_fields(_Rule()),
            "reason": "Amount exceeds $1,000",
            "source": "saturn_mcp",
        },
    )
    assert sealed is not None
    assert "raw_request" not in sealed
    assert sealed["shown"] == {"amount": 5800}
    assert isinstance(sealed["sealed"], str)
    visible = public_payload(sealed)
    assert visible is not None
    assert visible["sealed"] is True
    assert visible["arguments"] == {"amount": 5800}
    assert "note" not in str(visible)
    opened = open_json("org_test", sealed["sealed"])
    assert opened["arguments"]["note"] == "private body"
    stored = seal_arguments("org_test", {"amount": 5800, "note": "private body"})
    assert plain_arguments("org_test", stored)["note"] == "private body"
    assert seal_json("org_other", {"amount": 1}) != sealed["sealed"]


async def test_activity_and_admin_do_not_return_sealed_tool_content(client, monkeypatch):
    async def listed(endpoint, authorization=""):
        return [{"name": "repo.read", "description": "Read", "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}}}}]

    async def called(endpoint, name, arguments, authorization=""):
        return {"content": [{"type": "text", "text": "ok"}], "echo": arguments}

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.saturn.services.saturn_mcp.call_remote_tool", called)
    connected = await client.post(
        "/mcp-servers",
        json={"name": "GitHub", "endpoint": "https://api.githubcopilot.com/mcp", "authorization": "token-1"},
    )
    assert connected.status_code == 201, connected.text
    tool_id = next(item["id"] for item in connected.json()["catalog"] if item["name"] == "repo.read")
    created = await client.post("/tool-sets", json={"name": "Reads", "items": [{"tool_id": tool_id, "approval": False}]})
    access = await oauth_access(client, "Seal Agent")
    await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    agent_id = next(item["id"] for item in (await client.get("/agents")).json() if item["name"] == "Seal Agent")
    await client.patch(f"/agents/{agent_id}", json={"tool_set_id": created.json()["id"]})
    called_tool = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access}", "X-Saturn-Agent-Id": agent_id},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "repo.read", "arguments": {"repo": "saturn", "note": "secret note"}}},
    )
    assert called_tool.status_code == 200, called_tool.text
    activity = await client.get("/activity")
    assert activity.status_code == 200, activity.text
    requested = next(item for item in activity.json() if item["tool"] == "repo.read" and item["event"] == "tool_requested")
    assert requested["payload"]["encryption_status"] == "unavailable"
    assert "secret note" not in str(requested["payload"])
    assert "raw_request" not in requested["payload"]
    admin = await client.get("/admin/audit")
    assert admin.status_code == 200, admin.text
    admin_row = next(item for item in admin.json() if item["id"] == requested["id"])
    assert "sealed" not in (admin_row["payload"] or {})
    assert "arguments" not in (admin_row["payload"] or {})
    assert "note" not in str(admin_row["payload"])

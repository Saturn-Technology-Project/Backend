import pytest

from tests.oauth_access import oauth_tokens


def _claude(agents: list[dict]) -> list[dict]:
    return [item for item in agents if item["name"] == "Claude"]


@pytest.mark.asyncio
async def test_claude_reconnect_reuses_the_client_and_the_agent(client):
    first = await client.post(
        "/oauth/register",
        json={"client_name": "Claude", "redirect_uris": ["http://127.0.0.1:8765/callback"]},
    )
    second = await client.post(
        "/oauth/register",
        json={"client_name": "Claude", "redirect_uris": ["http://127.0.0.1:8765/callback"]},
    )
    assert first.status_code == 201
    assert second.json()["client_id"] == first.json()["client_id"]

    phone = await oauth_tokens(client, "Claude", redirect_uri="http://127.0.0.1:8765/callback")
    again = await oauth_tokens(client, "Claude", redirect_uri="http://127.0.0.1:8765/callback")
    assert phone[2] == again[2]

    opened = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {phone[0]}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "Claude"}},
        },
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["result"]["capabilities"]["tools"]["listChanged"] is True
    session_id = opened.headers["mcp-session-id"]
    created = _claude((await client.get("/agents")).json())
    assert len(created) == 1
    agent_id = created[0]["id"]

    reconnected = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {again[0]}"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {"clientInfo": {"name": "Claude"}},
        },
    )
    assert reconnected.status_code == 200, reconnected.text
    session_id = reconnected.headers["mcp-session-id"]
    still = _claude((await client.get("/agents")).json())
    assert [item["id"] for item in still] == [agent_id]

    other = await oauth_tokens(client, "Not Claude", redirect_uri="http://127.0.0.1:9321/callback")
    separate = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {other[0]}"},
        json={"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}},
    )
    assert separate.status_code == 200, separate.text
    named = {item["name"] for item in (await client.get("/agents")).json()}
    assert "Claude" in named
    assert "Not Claude" in named

    assigned = await client.post(
        "/tool-sets",
        json={"name": "Phone", "items": [{"tool_id": "tool_crm_get_customer", "approval": False}]},
    )
    assert assigned.status_code == 201, assigned.text
    saved = await client.patch(f"/agents/{agent_id}", json={"tool_set_id": assigned.json()["id"]})
    assert saved.status_code == 200, saved.text
    from app.features.mcp.saturn.services.mcp_sessions import mcp_sessions

    record = await mcp_sessions.get(session_id, (await client.get("/auth/me")).json()["id"], again[2])
    assert record is not None
    queued = record.queue.get_nowait()
    assert queued == {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_redirect", "new_redirect", "extra_redirects"),
    [
        ("https://claude.ai/api/mcp/auth_callback", "https://other.example/callback", []),
        (
            "https://claude.ai/api/mcp/auth_callback",
            "https://other.example/callback",
            ["https://claude.ai/api/mcp/auth_callback"],
        ),
        ("https://app.example/first", "https://app.example/second", []),
        ("http://127.0.0.1:8765/callback", "http://127.0.0.1:9876/callback", []),
    ],
    ids=["different-host", "overlapping-redirects", "same-host", "different-loopback-port"],
)
async def test_another_app_with_the_same_name_gets_its_own_empty_agent(
    client, owner_redirect, new_redirect, extra_redirects
):
    owner = await oauth_tokens(client, "Claude", redirect_uri=owner_redirect)
    first = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {owner[0]}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "Claude"}}},
    )
    assert first.status_code == 200, first.text
    agent_id = _claude((await client.get("/agents")).json())[0]["id"]
    created = await client.post(
        "/tool-sets",
        json={"name": "Phone", "items": [{"tool_id": "tool_crm_get_customer", "approval": False}]},
    )
    assert (await client.patch(f"/agents/{agent_id}", json={"tool_set_id": created.json()["id"]})).status_code == 200

    # A different client ID cannot inherit assignments, even with overlapping redirect hosts.
    intruder = await oauth_tokens(
        client,
        "Claude",
        redirect_uri=new_redirect,
        redirect_uris=[new_redirect, *extra_redirects],
    )
    assert intruder[2] != owner[2]
    joined = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {intruder[0]}"},
        json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"clientInfo": {"name": "Claude"}}},
    )
    assert joined.status_code == 200, joined.text
    listed = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {intruder[0]}"},
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    )
    assert listed.json()["result"]["tools"] == []

    agents = _claude((await client.get("/agents")).json())
    assert len(agents) == 2
    original = next(item for item in agents if item["id"] == agent_id)
    assert original["client_id"] == owner[2]
    assert original["tool_set_id"] == created.json()["id"]
    still = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {owner[0]}"},
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
    )
    assert [tool["name"] for tool in still.json()["result"]["tools"]] == ["crm.get_customer"]

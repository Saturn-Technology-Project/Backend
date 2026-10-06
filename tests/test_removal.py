async def test_only_admins_can_remove_agents_and_mcp_servers(client):
    invited = await client.post(
        "/org/invites",
        json={
            "email": "operator@saturn.ai",
            "name": "Operator",
            "title": "Operator",
            "role": "operator",
            "agent_ids": ["agent_customer_ops"],
        },
    )
    assert invited.status_code == 201, invited.text
    accepted = await client.post(
        "/auth/accept-invite",
        json={"token": invited.json()["token"], "password": "password1"},
    )
    assert accepted.status_code == 200, accepted.text
    denied = {"Authorization": f"Bearer {accepted.json()['token']}"}

    blocked = await client.delete("/agents/agent_customer_ops", headers=denied)
    assert blocked.status_code == 403
    blocked_mcp = await client.delete("/mcp-servers/mcp_email", headers=denied)
    assert blocked_mcp.status_code == 403

    removed = await client.delete("/agents/agent_customer_ops")
    assert removed.status_code == 204, removed.text
    missing = await client.get("/agents/agent_customer_ops")
    assert missing.status_code == 404

    removed_mcp = await client.delete("/mcp-servers/mcp_email")
    assert removed_mcp.status_code == 204, removed_mcp.text
    listing = await client.get("/mcp-servers")
    assert listing.status_code == 200, listing.text
    assert "mcp_email" not in {item["id"] for item in listing.json()}

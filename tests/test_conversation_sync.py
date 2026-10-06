async def test_messages_sync_for_the_same_user_and_a_revise_drops_later_turns(client):
    created = await client.post(
        "/agents/agent_customer_ops/messages",
        json={"input": "alpha"},
    )
    assert created.status_code == 201, created.text
    first = created.json()
    assert first["status"] == "queued"

    claimed = await client.put(
        f"/agents/agent_customer_ops/messages/{first['id']}",
        json={"input": "alpha", "output": "echo alpha", "status": "completed", "actions": []},
    )
    assert claimed.status_code == 200, claimed.text

    second = await client.put(
        "/agents/agent_customer_ops/messages/msg_beta",
        json={"input": "beta", "output": "echo beta", "status": "completed", "actions": []},
    )
    assert second.status_code == 200, second.text

    listed = await client.get("/agents/agent_customer_ops/messages")
    assert listed.status_code == 200, listed.text
    assert [item["input"] for item in listed.json()] == ["alpha", "beta"]

    revised = await client.put(
        f"/agents/agent_customer_ops/messages/{first['id']}",
        json={"input": "gamma", "output": "", "status": "running", "actions": []},
    )
    assert revised.status_code == 200, revised.text
    after = await client.get("/agents/agent_customer_ops/messages")
    body = after.json()
    assert [item["id"] for item in body] == [first["id"]]
    assert body[0]["input"] == "gamma"

    again = await client.put(
        f"/agents/agent_customer_ops/messages/{first['id']}",
        json={"input": "gamma", "output": "", "status": "running", "actions": []},
    )
    assert again.status_code == 409


async def test_agent_edit_permission_limits_who_can_change_settings(client):
    invited = await client.post(
        "/org/invites",
        json={
            "email": "editor@saturn.ai",
            "name": "Editor",
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
    headers = {"Authorization": f"Bearer {accepted.json()['token']}"}
    members = (await client.get("/org/members")).json()
    editor = next(item for item in members if item["email"] == "editor@saturn.ai")
    assert editor["agent_edit_ids"] is None

    allowed = await client.patch(
        "/agents/agent_customer_ops",
        headers=headers,
        json={"description": "still allowed"},
    )
    assert allowed.status_code == 200, allowed.text

    locked = await client.patch(
        f"/org/members/{editor['id']}",
        json={"agent_ids": ["agent_customer_ops"], "agent_edit_ids": []},
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["agent_edit_ids"] == []

    denied = await client.patch(
        "/agents/agent_customer_ops",
        headers=headers,
        json={"description": "blocked"},
    )
    assert denied.status_code == 403

    me = await client.get("/auth/me", headers=headers)
    assert me.json()["agent_edit_ids"] == []

async def test_app_agents_sync_apart_from_cloud_agents(client):
    before = [item["id"] for item in (await client.get("/agents")).json()]
    saved = await client.put(
        "/app-state",
        json={
            "agents": [
                {
                    "id": "saturn-agent",
                    "name": "Built in",
                },
                {
                    "id": "agent_local",
                    "name": "Desk",
                    "scope": "personal",
                    "instructions": "local only",
                    "mcp_connection_ids": ["mcp_local"],
                },
                {
                    "id": "agent_old",
                    "name": "Old",
                },
            ],
            "messages": [
                {
                    "id": "msg_1",
                    "agent_id": "agent_local",
                    "input": "hello",
                    "output": "hi",
                    "status": "completed",
                }
            ],
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert [item["id"] for item in body["agents"]] == ["agent_local"]
    assert body["agents"][0]["scope"] == "personal"
    assert body["agents"][0]["mcp_connection_ids"] == ["mcp_local"]
    assert body["messages"][0]["input"] == "hello"

    again = (await client.get("/app-state")).json()
    assert again["agents"][0]["name"] == "Desk"
    assert [item["id"] for item in (await client.get("/agents")).json()] == before
    assert (await client.get("/activity")).json() == []


async def test_team_agents_sync_for_the_org_and_personal_agents_do_not(client):
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

    team = await client.put(
        "/org-app-state",
        json={"agents": [{"id": "agent_team", "name": "Ops", "scope": "team", "instructions": "shared"}]},
    )
    assert team.status_code == 200, team.text
    assert team.json()["agents"][0]["scope"] == "team"

    personal = await client.put(
        "/app-state",
        json={
            "agents": [{"id": "agent_mine", "name": "Desk", "scope": "personal"}],
            "messages": [
                {"id": "msg_team", "agent_id": "agent_team", "input": "only me", "output": "ok", "status": "completed"},
                {"id": "msg_mine", "agent_id": "agent_mine", "input": "desk", "status": "completed"},
            ],
        },
    )
    assert personal.status_code == 200, personal.text
    assert [item["id"] for item in personal.json()["agents"]] == ["agent_mine"]
    assert personal.json()["agents"][0]["scope"] == "personal"
    assert {item["id"] for item in personal.json()["messages"]} == {"msg_team", "msg_mine"}

    seen = await client.get("/org-app-state", headers=viewer)
    assert seen.status_code == 200, seen.text
    assert [item["name"] for item in seen.json()["agents"]] == ["Ops"]
    viewer_personal = await client.get("/app-state", headers=viewer)
    assert viewer_personal.json()["agents"] == []
    assert viewer_personal.json()["messages"] == []

    denied = await client.put("/org-app-state", headers=viewer, json={"agents": [{"id": "agent_no", "name": "Nope"}]})
    assert denied.status_code == 403
    still = await client.get("/org-app-state", headers=viewer)
    assert [item["id"] for item in still.json()["agents"]] == ["agent_team"]
    assert "agent_team" not in [item["id"] for item in (await client.get("/agents")).json()]

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
                    "runtime_base": "http://10.0.0.8:4700",
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
    assert body["agents"][0]["runtime_base"] == ""
    assert body["agents"][0]["mcp_connection_ids"] == ["mcp_local"]
    assert body["messages"][0]["input"] == "hello"

    again = (await client.get("/app-state")).json()
    assert again["agents"][0]["name"] == "Desk"
    assert again["tasks"] == []
    assert again["wakes"] == []
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
        json={"agents": [{"id": "agent_team", "name": "Ops", "scope": "team", "instructions": "shared", "runtime_base": "http://10.0.0.8:4700/ignored"}]},
    )
    assert team.status_code == 200, team.text
    assert team.json()["agents"][0]["scope"] == "team"
    assert team.json()["agents"][0]["runtime_base"] == "http://10.0.0.8:4700"

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


def _task(updated_at: str, summary: str) -> dict:
    return {
        "id": "task_1",
        "agent_id": "agent_local",
        "goal": "Review the portfolio",
        "status": "working",
        "steps": [{"id": "task_1:1", "title": "Read holdings", "status": "working"}],
        "checkpoint": {"at": updated_at, "summary": summary, "step_id": "task_1:1"},
        "artifacts": [],
        "run_ids": ["run_1"],
        "created_at": "2026-10-09T01:00:00Z",
        "updated_at": updated_at,
    }


async def test_tasks_survive_a_desktop_sync_and_keep_the_newer_checkpoint(client):
    saved = await client.put(
        "/app-state",
        json={
            "agents": [{"id": "agent_local", "name": "Desk", "scope": "personal"}],
            "tasks": [_task("2026-10-09T02:00:00Z", "Holdings read")],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["tasks"][0]["checkpoint"]["summary"] == "Holdings read"

    desktop = await client.put(
        "/app-state",
        json={"agents": [{"id": "agent_local", "name": "Desk", "scope": "personal"}], "messages": []},
    )
    assert desktop.json()["tasks"][0]["goal"] == "Review the portfolio"

    older = await client.put(
        "/app-state",
        json={
            "agents": [{"id": "agent_local", "name": "Desk", "scope": "personal"}],
            "tasks": [_task("2026-10-09T01:00:00Z", "Stale")],
        },
    )
    assert older.json()["tasks"][0]["checkpoint"]["summary"] == "Holdings read"

    newer = await client.put(
        "/app-state",
        json={
            "agents": [{"id": "agent_local", "name": "Desk", "scope": "personal"}],
            "tasks": [_task("2026-10-09T03:00:00Z", "Note written")],
        },
    )
    assert newer.json()["tasks"][0]["checkpoint"]["summary"] == "Note written"


def _wake(updated_at: str, goal: str) -> dict:
    return {
        "id": "wake_1",
        "agent_id": "agent_local",
        "goal": goal,
        "steps": [],
        "enabled": True,
        "kind": "schedule",
        "every": "day",
        "at": "08:00",
        "when": None,
        "threshold": None,
        "source": "",
        "next_at": "2026-10-10T08:00:00Z",
        "last_at": None,
        "last_error": "",
        "created_at": "2026-10-09T01:00:00Z",
        "updated_at": updated_at,
    }


async def test_wakes_survive_a_desktop_sync_and_keep_the_newer_schedule(client):
    saved = await client.put(
        "/app-state",
        json={
            "agents": [{"id": "agent_local", "name": "Desk", "scope": "personal"}],
            "wakes": [_wake("2026-10-09T02:00:00Z", "Morning review")],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["wakes"][0]["goal"] == "Morning review"

    desktop = await client.put(
        "/app-state",
        json={"agents": [{"id": "agent_local", "name": "Desk", "scope": "personal"}], "messages": []},
    )
    assert desktop.json()["wakes"][0]["goal"] == "Morning review"

    older = await client.put(
        "/app-state",
        json={
            "agents": [{"id": "agent_local", "name": "Desk", "scope": "personal"}],
            "wakes": [_wake("2026-10-09T01:00:00Z", "Stale")],
        },
    )
    assert older.json()["wakes"][0]["goal"] == "Morning review"


async def test_memories_survive_a_desktop_sync(client):
    saved = await client.put(
        "/app-state",
        json={
            "agents": [{"id": "agent_local", "name": "Desk", "scope": "personal"}],
            "memories": [{"id": "mem_1", "agent_id": "agent_local", "body": "Prefers the morning brief", "created_at": "2026-10-09T02:00:00Z"}],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["memories"][0]["body"] == "Prefers the morning brief"

    desktop = await client.put(
        "/app-state",
        json={"agents": [{"id": "agent_local", "name": "Desk", "scope": "personal"}], "messages": []},
    )
    assert desktop.json()["memories"][0]["body"] == "Prefers the morning brief"

"""Approval rules cannot be weakened by a refused edit, a removed group, or a change made after approval."""

from tests.policy_helpers import activate_created
from tests.test_approval_hardening import _payments
from tests.test_quorum import _auth, _quorum_tool, _request


async def test_a_refused_approval_policy_edit_changes_nothing(client):
    created = await client.post(
        "/org/approval-policies", json={"name": "Strict", "required_count": 1, "allow_requester": False}
    )
    assert created.status_code == 201, created.text
    before = created.json()

    # One bad field. The good fields in the same request must not be saved.
    refused = await client.patch(
        f"/org/approval-policies/{before['id']}",
        json={"name": "Loose", "allow_requester": True, "status": "disabled", "expires_minutes": 0},
    )
    assert refused.status_code == 400, refused.text
    after = next(item for item in (await client.get("/org/approval-policies")).json() if item["id"] == before["id"])
    assert after["name"] == "Strict"
    assert after["allow_requester"] is False
    assert after["status"] == "active"
    assert after["version"] == before["version"]


async def test_a_group_an_approval_policy_depends_on_cannot_be_removed(client, monkeypatch):
    calls, people, access, agent_id, _policy, group_id = await _quorum_tool(client, monkeypatch)
    approval_policy = (await client.get("/org/approval-policies")).json()[0]

    refused = await client.delete(f"/org/groups/{group_id}")
    assert refused.status_code == 409, refused.text
    assert "Two finance approvers" in refused.json()["detail"]

    # Emptying the group does not delete it either, so the policy keeps asking for its members.
    for employee_id, _token in people[:3]:
        removed = await client.delete(f"/org/groups/{group_id}/members/{employee_id}")
        assert removed.status_code == 200, removed.text
    assert group_id in {item["id"] for item in (await client.get("/org/groups")).json()}
    kept = next(item for item in (await client.get("/org/approval-policies")).json() if item["id"] == approval_policy["id"])
    assert kept["required_count"] == 2 and kept["group_id"] == group_id
    assert kept["version"] == approval_policy["version"]

    # Nobody can approve a request under it until the group has members again.
    body = await _request(client, access, agent_id, {"amount": 900})
    approval_id = body["error"]["data"]["approval_id"]
    outsider = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))
    assert outsider.status_code == 403, outsider.text
    assert calls["n"] == 0

    # Turning the policy off is the explicit step that lets the group go.
    disabled = await client.patch(f"/org/approval-policies/{approval_policy['id']}", json={"status": "disabled"})
    assert disabled.status_code == 200, disabled.text
    assert (await client.delete(f"/org/groups/{group_id}")).status_code == 204


async def test_a_disabled_group_cannot_vote(client, monkeypatch):
    calls, people, access, agent_id, _policy, group_id = await _quorum_tool(client, monkeypatch)
    body = await _request(client, access, agent_id, {"amount": 900})
    approval_id = body["error"]["data"]["approval_id"]
    assert (await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))).status_code == 200
    disabled = await client.patch(f"/org/groups/{group_id}", json={"status": "disabled"})
    assert disabled.status_code == 200, disabled.text
    second = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[1][1]))
    assert second.status_code == 403, second.text
    assert calls["n"] == 0


async def test_the_last_vote_does_not_run_a_call_for_a_disabled_requester(client, monkeypatch):
    calls, seen = {"n": 0}, []

    def body(groups):
        return {"name": "Two", "required_count": 2, "allow_requester": False}

    people, _groups, _access, _agent_id = await _payments(client, monkeypatch, calls, seen, policy_body=body)
    # A second member connects their own agent and asks.
    from tests.oauth_access import oauth_access

    invited = await client.post("/org/invites", json={"email": "asker@saturn.ai", "name": "Asker", "role": "operator"})
    assert invited.status_code == 201, invited.text
    accepted = await client.post("/auth/accept-invite", json={"token": invited.json()["token"], "password": "password1"})
    assert accepted.status_code == 200, accepted.text
    asker = accepted.json()["user"]["employee"]["id"]
    access = await oauth_access(client, "Asker Agent", email="asker@saturn.ai", password="password1")
    opened = await client.post(
        "/mcp", headers=_auth(access), json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert opened.status_code == 200, opened.text
    agent_id = next(item["id"] for item in (await client.get("/agents")).json() if item["name"] == "Asker Agent")
    tool_set = (await client.get("/tool-sets")).json()[0]["id"]
    assert (await client.patch(f"/agents/{agent_id}", json={"tool_set_id": tool_set})).status_code == 200

    held = await _request(client, access, agent_id, {"amount": 5})
    approval_id = held["error"]["data"]["approval_id"]
    assert (await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))).status_code == 200
    assert (await client.patch(f"/org/members/{asker}", json={"status": "disabled"})).status_code == 200

    last = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[1][1]))
    assert last.status_code == 409, last.text
    assert calls["n"] == 0
    assert (await client.get(f"/approvals/{approval_id}")).json()["status"] == "invalidated"


async def test_an_approved_call_does_not_run_after_its_policy_changes(client, monkeypatch):
    calls, seen = {"n": 0}, []
    people, _groups, access, agent_id = await _payments(client, monkeypatch, calls, seen)
    created = await client.post(
        "/policies",
        json={"name": "Review payments", "rules": [{"tool": "pay.send", "condition": "true", "decision": "approval_required"}]},
    )
    policy = await activate_created(client, created)
    body = await _request(client, access, agent_id, {"amount": 6})
    approval_id = body["error"]["data"]["approval_id"]
    run_id = body["error"]["data"]["saturnRunId"]
    assert (await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))).status_code == 200

    # The rule is tightened after approval and before the agent comes back.
    stricter = await client.post("/org/approval-policies", json={"name": "Two people", "required_count": 2})
    assert stricter.status_code == 201, stricter.text
    edited = await client.patch(
        f"/policies/{policy['id']}",
        json={
            "rules": [
                {
                    "tool": "pay.send",
                    "condition": "true",
                    "decision": "approval_required",
                    "approval_policy_id": stricter.json()["id"],
                }
            ]
        },
    )
    assert edited.status_code == 200, edited.text
    assert (await client.post(f"/policies/{policy['id']}/validate")).status_code == 200
    assert (await client.post(f"/policies/{policy['id']}/activate", json={"confirm": True})).status_code == 200

    resumed = await _request(client, access, agent_id, {"amount": 6}, run_id=run_id)
    assert resumed["error"]["data"]["status"] == "invalidated", resumed
    assert calls["n"] == 0
    assert (await client.get(f"/approvals/{approval_id}")).json()["status"] == "invalidated"


async def test_a_refused_member_edit_does_not_change_the_role(client):
    invited = await client.post("/org/invites", json={"email": "kept@saturn.ai", "name": "Kept", "role": "operator"})
    assert invited.status_code == 201, invited.text
    accepted = await client.post("/auth/accept-invite", json={"token": invited.json()["token"], "password": "password1"})
    assert accepted.status_code == 200, accepted.text
    employee_id = accepted.json()["user"]["employee"]["id"]

    refused = await client.patch(
        f"/org/members/{employee_id}", json={"role": "viewer", "agent_ids": ["agent_missing"]}
    )
    assert refused.status_code == 404, refused.text
    member = next(item for item in (await client.get("/org/members")).json() if item["id"] == employee_id)
    assert member["role"] == "operator"

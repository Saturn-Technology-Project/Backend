"""Approvals are re-checked against the rules in force when they are decided (audit 2026-11-08)."""

from tests.policy_helpers import activate_created, create_active_policy
from tests.test_approval_hardening import _payments
from tests.test_quorum import _auth, _quorum_tool, _request


async def test_quorum_does_not_execute_after_a_deny_policy_is_added(client, monkeypatch):
    calls, people, access, agent_id, _policy, _group = await _quorum_tool(client, monkeypatch)
    body = await _request(client, access, agent_id, {"amount": 900})
    approval_id = body["error"]["data"]["approval_id"]
    first = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))
    assert first.status_code == 200, first.text

    # Security blocks the tool while the request waits for its second vote.
    await create_active_policy(
        client, {"name": "Freeze payments", "rules": [{"tool": "pay.send", "condition": "true", "decision": "deny"}]}
    )
    second = await client.post(
        f"/approvals/{approval_id}/approve", headers=_auth(people[1][1]), json={"arguments": {"amount": 900}}
    )
    assert second.status_code == 403, second.text
    assert calls["n"] == 0
    assert (await client.get(f"/approvals/{approval_id}")).json()["status"] == "rejected"


async def test_quorum_deny_on_real_arguments_applies(client, monkeypatch):
    calls, people, access, agent_id, _policy, _group = await _quorum_tool(client, monkeypatch)
    body = await _request(client, access, agent_id, {"amount": 900})
    approval_id = body["error"]["data"]["approval_id"]
    assert (await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))).status_code == 200
    # A rule that only matches the real amount: a replay of the stored decision would miss it.
    await create_active_policy(
        client,
        {
            "name": "Cap payments",
            "rules": [
                {
                    "tool": "pay.send",
                    "decision": "deny",
                    "conditions": [{"field": "amount", "operator": "gt", "value": 500}],
                },
                {"tool": "pay.send", "condition": "true", "decision": "approval_required"},
            ],
        },
    )
    second = await client.post(
        f"/approvals/{approval_id}/approve", headers=_auth(people[1][1]), json={"arguments": {"amount": 900}}
    )
    assert second.status_code == 403, second.text
    assert calls["n"] == 0


async def test_quorum_does_not_execute_a_tool_removed_from_the_tool_set(client, monkeypatch):
    calls, people, access, agent_id, _policy, _group = await _quorum_tool(client, monkeypatch)
    body = await _request(client, access, agent_id, {"amount": 900})
    approval_id = body["error"]["data"]["approval_id"]
    assert (await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))).status_code == 200
    # The agent loses the tool set that exposed the tool.
    unassigned = await client.patch(f"/agents/{agent_id}", json={"tool_set_id": None})
    assert unassigned.status_code == 200, unassigned.text
    second = await client.post(
        f"/approvals/{approval_id}/approve", headers=_auth(people[1][1]), json={"arguments": {"amount": 900}}
    )
    assert second.status_code == 409, second.text
    assert calls["n"] == 0
    assert (await client.get(f"/approvals/{approval_id}")).json()["status"] == "invalidated"


async def test_quorum_size_change_invalidates_pending_requests(client, monkeypatch):
    calls, people, access, agent_id, _policy, _group = await _quorum_tool(client, monkeypatch)
    body = await _request(client, access, agent_id, {"amount": 900})
    approval_id = body["error"]["data"]["approval_id"]
    assert (await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))).status_code == 200
    approval_policy_id = (await client.get("/org/approval-policies")).json()[0]["id"]
    raised = await client.patch(f"/org/approval-policies/{approval_policy_id}", json={"required_count": 3})
    assert raised.status_code == 200, raised.text
    second = await client.post(
        f"/approvals/{approval_id}/approve", headers=_auth(people[1][1]), json={"arguments": {"amount": 900}}
    )
    assert second.status_code == 409, second.text
    assert calls["n"] == 0


async def _single_with_policy(client, monkeypatch):
    calls, seen = {"n": 0}, []
    people, _groups, access, agent_id = await _payments(client, monkeypatch, calls, seen)
    created = await client.post(
        "/policies",
        json={"name": "Review payments", "rules": [{"tool": "pay.send", "condition": "true", "decision": "approval_required"}]},
    )
    policy = await activate_created(client, created)
    return calls, people, access, agent_id, policy["id"]


async def test_single_approver_is_invalidated_when_the_policy_changes(client, monkeypatch):
    calls, people, access, agent_id, policy_id = await _single_with_policy(client, monkeypatch)
    body = await _request(client, access, agent_id, {"amount": 6})
    approval_id = body["error"]["data"]["approval_id"]
    edited = await client.patch(
        f"/policies/{policy_id}",
        json={"rules": [{"tool": "pay.send", "condition": "true", "decision": "allow"}]},
    )
    assert edited.status_code == 200, edited.text
    assert (await client.post(f"/policies/{policy_id}/validate")).status_code == 200
    assert (await client.post(f"/policies/{policy_id}/activate", json={"confirm": True})).status_code == 200
    approved = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))
    assert approved.status_code == 409, approved.text
    assert (await client.get(f"/approvals/{approval_id}")).json()["status"] == "invalidated"
    assert calls["n"] == 0


async def test_single_approver_unchanged_policy_still_approves(client, monkeypatch):
    calls, people, access, agent_id, _policy_id = await _single_with_policy(client, monkeypatch)
    body = await _request(client, access, agent_id, {"amount": 6})
    approval_id = body["error"]["data"]["approval_id"]
    approved = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))
    assert approved.status_code == 200, approved.text
    resumed = await _request(client, access, agent_id, {"amount": 6}, run_id=body["error"]["data"]["saturnRunId"])
    assert resumed["result"]["tool"] == "pay.send"
    assert calls["n"] == 1

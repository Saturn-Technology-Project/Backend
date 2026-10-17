from datetime import timedelta

from tests.oauth_access import oauth_access


async def _people(client):
    async def member(email: str, role: str = "operator"):
        invited = await client.post(
            "/org/invites",
            json={"email": email, "name": email.split("@")[0], "role": role},
        )
        assert invited.status_code == 201, invited.text
        accepted = await client.post(
            "/auth/accept-invite",
            json={"token": invited.json()["token"], "password": "password1"},
        )
        assert accepted.status_code == 200, accepted.text
        body = accepted.json()
        return body["user"]["employee"]["id"], body["token"]

    first = await member("approver-one@saturn.ai")
    second = await member("approver-two@saturn.ai")
    third = await member("approver-three@saturn.ai")
    viewer = await member("approver-view@saturn.ai", "viewer")
    return first, second, third, viewer


async def _quorum_tool(client, monkeypatch, *, expires_minutes=None, allow_requester=False):
    calls = {"n": 0}

    async def listed(endpoint, authorization=""):
        return [
            {"name": "pay.send", "description": "Send", "inputSchema": {"type": "object", "properties": {}}},
        ]

    async def called(endpoint, name, arguments, authorization=""):
        calls["n"] += 1
        return {"content": [{"type": "text", "text": "ok"}], "tool": name, "echo": arguments}

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.saturn.services.saturn_mcp.call_remote_tool", called)

    connected = await client.post(
        "/mcp-servers",
        json={"name": "Payments", "endpoint": "https://mcp.stripe.com/mcp", "authorization": "token-1"},
    )
    assert connected.status_code == 201, connected.text
    tool_id = connected.json()["catalog"][0]["id"]
    created = await client.post(
        "/tool-sets",
        json={"name": "Payments", "items": [{"tool_id": tool_id, "approval": True}]},
    )
    assert created.status_code == 201, created.text
    people = await _people(client)
    group = await client.post("/org/groups", json={"name": "Finance Approvers", "description": "Payments"})
    assert group.status_code == 201, group.text
    group_id = group.json()["id"]
    for employee_id, _token in people[:3]:
        added = await client.post(f"/org/groups/{group_id}/members", json={"employee_id": employee_id})
        assert added.status_code == 200, added.text
    policy = await client.post(
        "/org/approval-policies",
        json={
            "name": "Two finance approvers",
            "required_count": 2,
            "group_id": group_id,
            "allow_requester": allow_requester,
            "expires_minutes": expires_minutes,
        },
    )
    assert policy.status_code == 201, policy.text
    rule = await client.post(
        "/policies",
        json={
            "name": "High value payment",
            "rules": [
                {
                    "tool": "pay.send",
                    "condition": "true",
                    "decision": "approval_required",
                    "approval_policy_id": policy.json()["id"],
                }
            ],
        },
    )
    assert rule.status_code == 201, rule.text
    access = await oauth_access(client, "Pay Agent")
    opened = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert opened.status_code == 200, opened.text
    agent_id = next(item["id"] for item in (await client.get("/agents")).json() if item["name"] == "Pay Agent")
    assigned = await client.patch(f"/agents/{agent_id}", json={"tool_set_id": created.json()["id"]})
    assert assigned.status_code == 200, assigned.text
    return calls, people, access, agent_id, rule.json()["id"], group_id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _request(client, access, agent_id, arguments, *, run_id=""):
    payload = dict(arguments)
    if run_id:
        payload["saturnRunId"] = run_id
    held = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "pay.send", "arguments": payload},
        },
        headers={
            "Authorization": f"Bearer {access}",
            "X-Saturn-Agent-Id": agent_id,
        },
    )
    assert held.status_code == 200, held.text
    return held.json()


async def test_two_distinct_approvals_execute_once(client, monkeypatch):
    calls, people, access, agent_id, _policy, _group = await _quorum_tool(client, monkeypatch)
    body = await _request(client, access, agent_id, {"amount": 900})
    assert body["error"]["data"]["code"] == "approval_pending"
    assert body["error"]["data"]["approval_url"]
    approval_id = body["error"]["data"]["approval_id"]
    assert calls["n"] == 0

    admin = await client.post(f"/approvals/{approval_id}/approve")
    assert admin.status_code == 403, admin.text
    assert calls["n"] == 0

    outsider = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[3][1]))
    assert outsider.status_code == 403, outsider.text

    first = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "pending"
    assert first.json()["result"] == "1/2"
    assert calls["n"] == 0

    again = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))
    assert again.status_code == 409, again.text
    assert calls["n"] == 0

    listed = await client.get(f"/approvals/{approval_id}")
    assert listed.json()["approved_count"] == 1
    assert listed.json()["required_count"] == 2
    assert listed.json()["execution_state"] == "none"

    second = await client.post(
        f"/approvals/{approval_id}/approve",
        headers=_auth(people[1][1]),
        json={"arguments": {"amount": 900}},
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "completed"
    assert calls["n"] == 1

    resumed = await _request(client, access, agent_id, {"amount": 900}, run_id=body["error"]["data"]["saturnRunId"])
    assert resumed["result"]["tool"] == "pay.send"
    assert calls["n"] == 1

    events = {item["event"] for item in (await client.get("/activity")).json()}
    assert {
        "approval_requested",
        "approval_granted",
        "approval_quorum",
        "execution_claimed",
        "tool_executed",
        "policy_evaluated",
    } <= events


async def test_removed_member_vote_no_longer_counts(client, monkeypatch):
    calls, people, access, agent_id, _policy, group_id = await _quorum_tool(client, monkeypatch)
    body = await _request(client, access, agent_id, {"amount": 10})
    approval_id = body["error"]["data"]["approval_id"]
    first = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))
    assert first.json()["result"] == "1/2"
    removed = await client.delete(f"/org/groups/{group_id}/members/{people[0][0]}")
    assert removed.status_code == 200, removed.text
    second = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[1][1]))
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "pending"
    assert second.json()["result"] == "1/2"
    assert calls["n"] == 0
    third = await client.post(
        f"/approvals/{approval_id}/approve",
        headers=_auth(people[2][1]),
        json={"arguments": {"amount": 10}},
    )
    assert third.status_code == 200, third.text
    assert third.json()["status"] == "completed"
    assert calls["n"] == 1


async def test_reject_cancel_expiry_and_mismatch_do_not_execute(client, monkeypatch):
    calls, people, access, agent_id, policy_id, _group = await _quorum_tool(client, monkeypatch)
    rejected_body = await _request(client, access, agent_id, {"amount": 1})
    rejected_id = rejected_body["error"]["data"]["approval_id"]
    denied = await client.post(f"/approvals/{rejected_id}/reject", headers=_auth(people[0][1]))
    assert denied.status_code == 200, denied.text
    later = await client.post(f"/approvals/{rejected_id}/approve", headers=_auth(people[1][1]))
    assert later.status_code == 409

    cancelled_body = await _request(client, access, agent_id, {"amount": 2})
    cancelled_id = cancelled_body["error"]["data"]["approval_id"]
    cancelled = await client.post(f"/approvals/{cancelled_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert (await client.post(f"/approvals/{cancelled_id}/approve", headers=_auth(people[0][1]))).status_code == 409

    changed = await _request(client, access, agent_id, {"amount": 3})
    changed_id = changed["error"]["data"]["approval_id"]
    mismatched = await _request(client, access, agent_id, {"amount": 4}, run_id=changed["error"]["data"]["saturnRunId"])
    assert mismatched["error"]["data"]["code"] == "approval_args_mismatch"
    assert (await client.post(f"/approvals/{changed_id}/approve", headers=_auth(people[0][1]))).status_code == 409

    stale_body = await _request(client, access, agent_id, {"amount": 5})
    stale_id = stale_body["error"]["data"]["approval_id"]
    recorded = await client.post(f"/approvals/{stale_id}/approve", headers=_auth(people[0][1]))
    assert recorded.json()["status"] == "pending"
    renamed = await client.patch(f"/policies/{policy_id}", json={"name": "Changed payment policy"})
    assert renamed.status_code == 200, renamed.text
    blocked = await client.post(f"/approvals/{stale_id}/approve", headers=_auth(people[1][1]))
    assert blocked.status_code == 409, blocked.text
    assert calls["n"] == 0


async def test_expired_and_uncertain_upstream_do_not_retry(client, monkeypatch):
    calls, people, access, agent_id, _policy, _group = await _quorum_tool(
        client, monkeypatch, expires_minutes=30
    )
    expired_body = await _request(client, access, agent_id, {"amount": 8})
    expired_id = expired_body["error"]["data"]["approval_id"]
    from app.features.execution.approvals.services import approval_service as approval_module

    real_now = approval_module.utcnow
    monkeypatch.setattr(approval_module, "utcnow", lambda: real_now() + timedelta(hours=2))
    expired = await client.post(f"/approvals/{expired_id}/approve", headers=_auth(people[0][1]))
    assert expired.status_code == 409, expired.text
    monkeypatch.setattr(approval_module, "utcnow", real_now)

    async def timeout(endpoint, name, arguments, authorization=""):
        calls["n"] += 1
        raise TimeoutError("upstream timed out")

    monkeypatch.setattr("app.features.mcp.saturn.services.saturn_mcp.call_remote_tool", timeout)
    uncertain_body = await _request(client, access, agent_id, {"amount": 9})
    uncertain_id = uncertain_body["error"]["data"]["approval_id"]
    assert (await client.post(f"/approvals/{uncertain_id}/approve", headers=_auth(people[0][1]))).json()["status"] == "pending"
    failed = await client.post(
        f"/approvals/{uncertain_id}/approve",
        headers=_auth(people[1][1]),
        json={"arguments": {"amount": 9}},
    )
    assert failed.status_code == 409, failed.text
    assert calls["n"] == 1
    retry = await _request(client, access, agent_id, {"amount": 9}, run_id=uncertain_body["error"]["data"]["saturnRunId"])
    assert retry["error"]["message"] == "execution_uncertain"
    assert calls["n"] == 1


async def test_groups_are_an_enterprise_or_team_capability(client):
    owned = await client.post("/org/groups", json={"name": "Finance Approvers"})
    assert owned.status_code == 201, owned.text
    other = await client.post(
        "/admin/orgs",
        json={
            "name": "Other",
            "admin_email": "other-admin@saturn.ai",
            "admin_name": "Other",
            "admin_password": "password1",
            "plan": "team",
            "kind": "organization",
        },
    )
    assert other.status_code == 201, other.text
    assert other.json()["plan"] == "team"
    assert other.json()["kind"] == "organization"
    signed = await client.post("/auth/login", json={"email": "other-admin@saturn.ai", "password": "password1"})
    foreign_headers = _auth(signed.json()["token"])
    from tests.totp_access import finish_totp

    await finish_totp(client, foreign_headers)
    foreign_org = await client.get("/org", headers=foreign_headers)
    assert foreign_org.json()["organization"]["entitlements"]["groups"] is True
    hidden = await client.get(f"/org/groups/{owned.json()['id']}", headers=foreign_headers)
    assert hidden.status_code == 404
    assert (await client.get("/approvals", headers=foreign_headers)).json() == []

    blocked = await client.patch("/admin/orgs/org_saturn", json={"plan": "pro"})
    assert blocked.status_code == 200, blocked.text
    org = await client.get("/org")
    assert org.json()["organization"]["entitlements"]["groups"] is False
    assert org.json()["organization"]["entitlements"]["policies"] is True
    created = await client.post("/org/groups", json={"name": "Nope"})
    assert created.status_code == 403, created.text
    single = await client.post("/org/approval-policies", json={"name": "Solo", "required_count": 1})
    assert single.status_code == 201, single.text
    quorum = await client.post("/org/approval-policies", json={"name": "Pair", "required_count": 2})
    assert quorum.status_code == 403, quorum.text
    both = await client.post(
        "/org/approval-policies",
        json={
            "name": "Finance and Admin",
            "requirements": [
                {"group_id": owned.json()["id"], "required_count": 2},
                {"group_id": "grp_other", "required_count": 1},
            ],
        },
    )
    assert both.status_code == 403, both.text


async def test_payment_needs_finance_and_admin(client, monkeypatch):
    calls = {"n": 0}

    async def listed(endpoint, authorization=""):
        return [{"name": "pay.send", "description": "Send", "inputSchema": {"type": "object", "properties": {}}}]

    async def called(endpoint, name, arguments, authorization=""):
        calls["n"] += 1
        return {"content": [{"type": "text", "text": "ok"}], "tool": name, "echo": arguments}

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.saturn.services.saturn_mcp.call_remote_tool", called)
    connected = await client.post(
        "/mcp-servers",
        json={"name": "Payments", "endpoint": "https://mcp.stripe.com/mcp", "authorization": "token-1"},
    )
    assert connected.status_code == 201, connected.text
    tool_id = connected.json()["catalog"][0]["id"]
    created = await client.post(
        "/tool-sets",
        json={"name": "Payments", "items": [{"tool_id": tool_id, "approval": True}]},
    )
    assert created.status_code == 201, created.text
    people = await _people(client)
    admin_group = await client.post("/org/groups", json={"name": "Admin"})
    finance_group = await client.post("/org/groups", json={"name": "Finance"})
    assert admin_group.status_code == 201, admin_group.text
    assert finance_group.status_code == 201, finance_group.text
    admin_id = admin_group.json()["id"]
    finance_id = finance_group.json()["id"]
    assert (await client.post(f"/org/groups/{admin_id}/members", json={"employee_id": people[0][0]})).status_code == 200
    assert (await client.post(f"/org/groups/{finance_id}/members", json={"employee_id": people[1][0]})).status_code == 200
    assert (await client.post(f"/org/groups/{finance_id}/members", json={"employee_id": people[2][0]})).status_code == 200
    duplicate = await client.post(
        "/org/approval-policies",
        json={
            "name": "Twice",
            "requirements": [
                {"group_id": finance_id, "required_count": 2},
                {"group_id": finance_id, "required_count": 1},
            ],
        },
    )
    assert duplicate.status_code == 400, duplicate.text
    policy = await client.post(
        "/org/approval-policies",
        json={
            "name": "Finance 2 and Admin 1",
            "allow_requester": False,
            "requirements": [
                {"group_id": finance_id, "required_count": 2},
                {"group_id": admin_id, "required_count": 1},
            ],
        },
    )
    assert policy.status_code == 201, policy.text
    saved = policy.json()
    assert saved["required_count"] == 3
    assert saved["group_id"] is None
    assert [(item["group_name"], item["required_count"]) for item in saved["requirements"]] == [
        ("Finance", 2),
        ("Admin", 1),
    ]
    rule = await client.post(
        "/policies",
        json={
            "name": "High value payment",
            "rules": [
                {
                    "tool": "pay.send",
                    "condition": "true",
                    "decision": "approval_required",
                    "approval_policy_id": saved["id"],
                }
            ],
        },
    )
    assert rule.status_code == 201, rule.text
    access = await oauth_access(client, "Pay Agent")
    opened = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert opened.status_code == 200, opened.text
    agent_id = next(item["id"] for item in (await client.get("/agents")).json() if item["name"] == "Pay Agent")
    assert (await client.patch(f"/agents/{agent_id}", json={"tool_set_id": created.json()["id"]})).status_code == 200
    body = await _request(client, access, agent_id, {"amount": 900})
    assert body["error"]["data"]["code"] == "approval_pending"
    assert body["error"]["data"]["approval_url"]
    approval_id = body["error"]["data"]["approval_id"]

    finance_only = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[1][1]))
    assert finance_only.status_code == 200, finance_only.text
    assert finance_only.json()["status"] == "pending"
    assert finance_only.json()["result"] == "1/2+0/1"
    assert calls["n"] == 0
    listed = await client.get(f"/approvals/{approval_id}")
    assert [(item["group_name"], item["approved_count"], item["required_count"]) for item in listed.json()["requirements"]] == [
        ("Finance", 1, 2),
        ("Admin", 0, 1),
    ]

    admin_only = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))
    assert admin_only.status_code == 200, admin_only.text
    assert admin_only.json()["status"] == "pending"
    assert admin_only.json()["result"] == "1/2+1/1"
    assert calls["n"] == 0

    outsider = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[3][1]))
    assert outsider.status_code == 403, outsider.text

    second_finance = await client.post(
        f"/approvals/{approval_id}/approve",
        headers=_auth(people[2][1]),
        json={"arguments": {"amount": 900}},
    )
    assert second_finance.status_code == 200, second_finance.text
    assert second_finance.json()["status"] == "completed"
    assert calls["n"] == 1
    resumed = await _request(client, access, agent_id, {"amount": 900}, run_id=body["error"]["data"]["saturnRunId"])
    assert resumed["result"]["tool"] == "pay.send"
    assert calls["n"] == 1


async def test_deleting_a_group_drops_it_from_the_approval_policy(client):
    admin = await client.post("/org/groups", json={"name": "Admin"})
    finance = await client.post("/org/groups", json={"name": "Finance"})
    assert admin.status_code == 201, admin.text
    assert finance.status_code == 201, finance.text
    policy = await client.post(
        "/org/approval-policies",
        json={
            "name": "Both",
            "requirements": [
                {"group_id": finance.json()["id"], "required_count": 2},
                {"group_id": admin.json()["id"], "required_count": 1},
            ],
        },
    )
    assert policy.status_code == 201, policy.text
    removed = await client.delete(f"/org/groups/{finance.json()['id']}")
    assert removed.status_code == 204, removed.text
    names = [item["name"] for item in (await client.get("/org/groups")).json()]
    assert "Finance" not in names
    assert "Admin" in names
    saved = next(item for item in (await client.get("/org/approval-policies")).json() if item["id"] == policy.json()["id"])
    assert [(item["group_name"], item["required_count"]) for item in saved["requirements"]] == [("Admin", 1)]
    assert saved["required_count"] == 1
    missing = await client.delete(f"/org/groups/{finance.json()['id']}")
    assert missing.status_code == 404


async def test_removing_the_last_member_deletes_the_group(client):
    people = await _people(client)
    group = await client.post("/org/groups", json={"name": "Temporary"})
    assert group.status_code == 201, group.text
    group_id = group.json()["id"]
    added = await client.post(f"/org/groups/{group_id}/members", json={"employee_id": people[0][0]})
    assert added.status_code == 200, added.text
    kept = await client.post(f"/org/groups/{group_id}/members", json={"employee_id": people[1][0]})
    assert kept.status_code == 200, kept.text
    one_left = await client.delete(f"/org/groups/{group_id}/members/{people[0][0]}")
    assert one_left.status_code == 200, one_left.text
    assert group_id in {item["id"] for item in (await client.get("/org/groups")).json()}
    gone = await client.delete(f"/org/groups/{group_id}/members/{people[1][0]}")
    assert gone.status_code == 204, gone.text
    assert group_id not in {item["id"] for item in (await client.get("/org/groups")).json()}

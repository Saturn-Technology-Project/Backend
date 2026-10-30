import asyncio
import hmac
from datetime import datetime, timedelta, timezone

from app.core.ids import utcnow as real_now
from app.features.webhooks.services.destinations import require_webhook_url
from app.features.webhooks.services.signing import sign
from app.features.webhooks.services.webhook_service import _public_error
from fastapi import HTTPException
from tests.oauth_access import oauth_access
from tests.policy_helpers import activate_created
from tests.test_quorum import _auth, _quorum_tool, _request


def _parse(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _freeze(monkeypatch, moment: datetime) -> None:
    def now(moment=moment):
        return moment

    for path in (
        "app.features.execution.approvals.services.approval_service.utcnow",
        "app.features.mcp.saturn.services.saturn_mcp.utcnow",
        "app.features.execution.approvals.services.expiration.utcnow",
        "app.features.webhooks.services.webhook_service.utcnow",
    ):
        monkeypatch.setattr(path, now)


def _thaw(monkeypatch) -> None:
    for path in (
        "app.features.execution.approvals.services.approval_service.utcnow",
        "app.features.mcp.saturn.services.saturn_mcp.utcnow",
        "app.features.execution.approvals.services.expiration.utcnow",
        "app.features.webhooks.services.webhook_service.utcnow",
    ):
        monkeypatch.setattr(path, real_now)


def _capture(monkeypatch, responses: list[tuple[int | None, str]]):
    seen: list[tuple[bytes, dict]] = []

    async def send(_url, body, headers):
        seen.append((body, headers))
        code, error = responses.pop(0)
        return code, error

    monkeypatch.setattr("app.features.webhooks.services.webhook_service.send_webhook", send)
    monkeypatch.setattr("app.features.webhooks.services.webhook_service.backoff_seconds", lambda _attempt: 0)
    return seen


async def _endpoint(client, monkeypatch, responses=None):
    seen = _capture(monkeypatch, responses if responses is not None else [(200, "")])
    created = await client.post(
        "/org/webhooks",
        json={
            "url": "https://hooks.example.com/saturn",
            "events": ["approval.granted", "approval.rejected", "approval.expired"],
        },
    )
    assert created.status_code == 201, created.text
    return created.json(), seen


async def _deferred(client, monkeypatch, *, expires_minutes=None, resubmit_minutes=None):
    calls = {"n": 0}

    async def listed(endpoint, authorization=""):
        return [{"name": "pay.send", "description": "Send", "inputSchema": {"type": "object", "properties": {}}}]

    async def called(endpoint, name, arguments, authorization=""):
        calls["n"] += 1
        return {"content": [{"type": "text", "text": "ok"}], "echo": arguments}

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
    policy = await client.post(
        "/org/approval-policies",
        json={
            "name": "One approver",
            "required_count": 1,
            "allow_requester": True,
            "expires_minutes": expires_minutes,
            "resubmit_minutes": resubmit_minutes,
        },
    )
    assert policy.status_code == 201, policy.text
    rule = await client.post(
        "/policies",
        json={
            "name": "Pay",
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
    await activate_created(client, rule)
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
    return calls, access, agent_id


async def _load(client, approval_id):
    listed = await client.get("/approvals")
    assert listed.status_code == 200, listed.text
    return next(item for item in listed.json() if item["id"] == approval_id)


async def _deliveries(client, endpoint_id):
    await client.post("/org/webhooks/dispatch")
    listed = await client.get(f"/org/webhooks/{endpoint_id}/deliveries")
    assert listed.status_code == 200, listed.text
    return listed.json()


def test_signature_rejects_a_different_secret():
    body = b'{"id":"whe_1","type":"approval.granted"}'
    valid = sign("secret-value", "1700000000", body)
    assert valid.startswith("v1=")
    assert not hmac.compare_digest(valid, sign("other-secret", "1700000000", body))
    assert "s3cret" not in _public_error("upstream said s3cret", "s3cret")


def test_webhook_urls_block_private_and_metadata_destinations(monkeypatch):
    assert require_webhook_url("https://hooks.example.com/saturn") == "https://hooks.example.com/saturn"
    for url in (
        "http://hooks.example.com/saturn",
        "https://user:pass@hooks.example.com/saturn",
        "https://127.0.0.1/hook",
        "https://10.1.2.3/hook",
        "https://169.254.169.254/",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://localhost/hook",
    ):
        try:
            require_webhook_url(url)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError(url)
    monkeypatch.setattr(
        "app.features.mcp.servers.services.mcp_endpoint.addresses_for",
        lambda _host, _port: ["10.0.0.8"],
    )
    try:
        require_webhook_url("https://hooks.example.com/saturn")
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("resolved private address")


async def test_webhook_secret_is_shown_once_and_viewers_cannot_manage_it(client, monkeypatch):
    created, _seen = await _endpoint(client, monkeypatch)
    assert created["secret"]
    listed = await client.get("/org/webhooks")
    assert listed.status_code == 200, listed.text
    assert "secret" not in listed.json()[0]
    downloaded = await client.get(f"/org/webhooks/{created['id']}/secret")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.text.strip() == created["secret"]
    assert "attachment" in downloaded.headers["content-disposition"]
    rotated = await client.post(f"/org/webhooks/{created['id']}/rotate")
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["secret"] != created["secret"]
    again = await client.get(f"/org/webhooks/{created['id']}/secret")
    assert again.text.strip() == rotated.json()["secret"]
    assert "secret" not in (await client.get("/org/webhooks")).json()[0]
    missing = await client.get("/org/webhooks/wh_missing/deliveries")
    assert missing.status_code == 404
    blocked = await client.post("/org/webhooks", json={"url": "https://127.0.0.1/hook", "events": ["approval.granted"]})
    assert blocked.status_code == 400
    invited = await client.post(
        "/org/invites",
        json={"email": "webhook-view@saturn.ai", "name": "Viewer", "role": "viewer"},
    )
    assert invited.status_code == 201, invited.text
    accepted = await client.post(
        "/auth/accept-invite",
        json={"token": invited.json()["token"], "password": "password1"},
    )
    viewer = {"Authorization": f"Bearer {accepted.json()['token']}"}
    denied = await client.post(
        "/org/webhooks",
        json={"url": "https://hooks.example.com/other", "events": ["approval.granted"]},
        headers=viewer,
    )
    assert denied.status_code == 403
    hidden = await client.get("/org/webhooks", headers=viewer)
    assert hidden.status_code == 403
    secret_hidden = await client.get(f"/org/webhooks/{created['id']}/secret", headers=viewer)
    assert secret_hidden.status_code == 403


async def test_delivery_signs_the_body_and_retries_only_retryable_failures(client, monkeypatch):
    created, seen = await _endpoint(client, monkeypatch, [(200, "")])
    tested = await client.post(f"/org/webhooks/{created['id']}/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["status"] == "delivered"
    body, headers = seen[0]
    assert sign(created["secret"], headers["Saturn-Webhook-Timestamp"], body) == headers["Saturn-Webhook-Signature"]
    assert b"token" not in body
    assert created["secret"].encode() not in body
    disabled = await client.patch(f"/org/webhooks/{created['id']}", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text

    again, retries = await _endpoint(client, monkeypatch, [(500, ""), (429, ""), (None, "timeout"), (200, "")])
    calls, access, agent_id = await _deferred(client, monkeypatch)
    pending = await _request(client, access, agent_id, {"amount": 5})
    assert pending["error"]["code"] == -32001
    approved = await client.post(f"/approvals/{pending['error']['data']['approval_id']}/approve")
    assert approved.status_code == 200, approved.text
    for _ in range(4):
        await client.post("/org/webhooks/dispatch")
    delivered = await client.get(f"/org/webhooks/{again['id']}/deliveries")
    granted = [item for item in delivered.json() if item["event_type"] == "approval.granted"]
    assert len(granted) == 1
    assert granted[0]["status"] == "delivered"
    assert granted[0]["attempt_count"] == 4
    assert len(retries) == 4
    assert calls["n"] == 0
    await client.patch(f"/org/webhooks/{again['id']}", json={"enabled": False})

    permanent, permanent_seen = await _endpoint(client, monkeypatch, [(400, "no"), (200, "")])
    pending = await _request(client, access, agent_id, {"amount": 6})
    await client.post(f"/approvals/{pending['error']['data']['approval_id']}/reject")
    await client.post("/org/webhooks/dispatch")
    await client.post("/org/webhooks/dispatch")
    assert len(permanent_seen) == 1
    failed = await client.get(f"/org/webhooks/{permanent['id']}/deliveries")
    rejected = next(item for item in failed.json() if item["event_type"] == "approval.rejected")
    assert rejected["status"] == "dead"
    retried = await client.post(f"/org/webhooks/deliveries/{rejected['id']}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "delivered"
    assert len(permanent_seen) == 2


async def test_redirect_is_not_followed(client, monkeypatch):
    _created, seen = await _endpoint(client, monkeypatch, [(302, "redirect not followed")])
    calls, access, agent_id = await _deferred(client, monkeypatch)
    pending = await _request(client, access, agent_id, {"amount": 1})
    await client.post(f"/approvals/{pending['error']['data']['approval_id']}/approve")
    await client.post("/org/webhooks/dispatch")
    await client.post("/org/webhooks/dispatch")
    assert len(seen) == 1


async def test_granted_webhook_waits_for_quorum_and_is_not_duplicated(client, monkeypatch):
    created, seen = await _endpoint(client, monkeypatch, [(200, "")])
    calls, people, access, agent_id, _policy, _group = await _quorum_tool(client, monkeypatch)
    pending = await _request(client, access, agent_id, {"amount": 5})
    approval_id = pending["error"]["data"]["approval_id"]
    first = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))
    assert first.json()["status"] == "pending"
    await client.post("/org/webhooks/dispatch")
    assert seen == []
    second = await client.post(
        f"/approvals/{approval_id}/approve",
        headers=_auth(people[1][1]),
        json={"arguments": {"amount": 5}},
    )
    assert second.status_code == 200, second.text
    await client.post("/org/webhooks/dispatch")
    await client.post("/org/webhooks/dispatch")
    assert len(seen) == 1
    assert calls["n"] == 1
    rows = await client.get(f"/org/webhooks/{created['id']}/deliveries")
    granted = [item for item in rows.json() if item["event_type"] == "approval.granted"]
    assert len(granted) == 1
    assert b"arguments" not in seen[0][0]


async def test_pending_and_resubmit_expiration(client, monkeypatch):
    window = await client.get("/org/approval-window")
    assert window.json() == {"pending_ttl_minutes": 1440, "resubmit_ttl_minutes": 1440}
    too_long = await client.patch("/org/approval-window", json={"pending_ttl_minutes": 10081})
    assert too_long.status_code == 400
    capped = await client.patch("/org/approval-window", json={"pending_ttl_minutes": 10080, "resubmit_ttl_minutes": 60})
    assert capped.status_code == 200, capped.text
    calls, access, agent_id = await _deferred(client, monkeypatch, expires_minutes=60, resubmit_minutes=15)
    early = await _request(client, access, agent_id, {"amount": 5})
    early_id = early["error"]["data"]["approval_id"]
    early_row = await _load(client, early_id)
    created_at = _parse(early_row["created_at"])
    expires_at = _parse(early_row["expires_at"])
    assert timedelta(minutes=59) < expires_at - created_at <= timedelta(minutes=60, seconds=5)

    _freeze(monkeypatch, expires_at - timedelta(seconds=2))
    still_open = await client.post(f"/approvals/{early_id}/approve")
    assert still_open.status_code == 200, still_open.text
    _thaw(monkeypatch)
    approved = await _load(client, early_id)
    assert approved["status"] == "approved"
    assert approved["execution_state"] == "none"
    resubmit_at = _parse(approved["resubmit_expires_at"])
    resolved_at = _parse(approved["resolved_at"])
    assert timedelta(minutes=14) < resubmit_at - resolved_at <= timedelta(minutes=15, seconds=5)

    _freeze(monkeypatch, resubmit_at - timedelta(seconds=2))
    ran = await _request(client, access, agent_id, {"amount": 5}, run_id=early["error"]["data"]["saturnRunId"])
    assert "result" in ran
    assert calls["n"] == 1
    _thaw(monkeypatch)
    again = await _request(client, access, agent_id, {"amount": 5}, run_id=early["error"]["data"]["saturnRunId"])
    assert "result" in again
    assert calls["n"] == 1

    mismatch = await _request(client, access, agent_id, {"amount": 8})
    mismatch_id = mismatch["error"]["data"]["approval_id"]
    assert (await client.post(f"/approvals/{mismatch_id}/approve")).status_code == 200
    mismatched = await _request(client, access, agent_id, {"amount": 9}, run_id=mismatch["error"]["data"]["saturnRunId"])
    assert mismatched["error"]["code"] == -32004
    assert calls["n"] == 1

    pending = await _request(client, access, agent_id, {"amount": 3})
    pending_id = pending["error"]["data"]["approval_id"]
    pending_row = await _load(client, pending_id)
    _freeze(monkeypatch, _parse(pending_row["expires_at"]))
    late = await client.post(f"/approvals/{pending_id}/approve")
    assert late.status_code == 409, late.text
    _thaw(monkeypatch)
    revived = await client.post(f"/approvals/{pending_id}/approve")
    assert revived.status_code == 409
    expired_call = await _request(client, access, agent_id, {"amount": 3}, run_id=pending["error"]["data"]["saturnRunId"])
    assert expired_call["error"]["code"] == -32006
    assert expired_call["error"]["data"]["reason"] == "APPROVAL_EXPIRED"
    assert expired_call["error"]["data"]["phase"] == "pending"
    assert calls["n"] == 1

    waiting = await _request(client, access, agent_id, {"amount": 4})
    waiting_id = waiting["error"]["data"]["approval_id"]
    assert (await client.post(f"/approvals/{waiting_id}/approve")).status_code == 200
    waiting_row = await _load(client, waiting_id)
    _freeze(monkeypatch, _parse(waiting_row["resubmit_expires_at"]))
    blocked = await _request(client, access, agent_id, {"amount": 4}, run_id=waiting["error"]["data"]["saturnRunId"])
    assert blocked["error"]["code"] == -32006
    assert blocked["error"]["data"]["phase"] == "resubmit"
    assert calls["n"] == 1
    _thaw(monkeypatch)

    listed = await _load(client, pending_id)
    assert listed["status"] == "expired"
    unknown = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "missing.tool", "arguments": {}}},
        headers={
            "Authorization": f"Bearer {access}",
            "X-Saturn-Agent-Id": agent_id,
        },
    )
    assert unknown.json()["error"]["code"] == -32003
    rejected = await _request(client, access, agent_id, {"amount": 11})
    rejected_id = rejected["error"]["data"]["approval_id"]
    assert (await client.post(f"/approvals/{rejected_id}/reject")).status_code == 200
    rejected_call = await _request(client, access, agent_id, {"amount": 11}, run_id=rejected["error"]["data"]["saturnRunId"])
    assert rejected_call["error"]["code"] == -32002
    assert rejected_call["error"]["message"].startswith("approval_rejected")


async def test_expired_event_is_emitted_once(client, monkeypatch):
    created, seen = await _endpoint(client, monkeypatch, [(200, ""), (200, "")])
    _calls, access, agent_id = await _deferred(client, monkeypatch, expires_minutes=30)
    pending = await _request(client, access, agent_id, {"amount": 2})
    approval_id = pending["error"]["data"]["approval_id"]
    row = await _load(client, approval_id)
    _freeze(monkeypatch, _parse(row["expires_at"]))
    listed = await client.get("/approvals")
    assert next(item["status"] for item in listed.json() if item["id"] == approval_id) == "expired"
    await client.get("/approvals")
    await client.post("/org/webhooks/dispatch")
    await client.post("/org/webhooks/dispatch")
    rows = await client.get(f"/org/webhooks/{created['id']}/deliveries")
    expired = [item for item in rows.json() if item["event_type"] == "approval.expired"]
    assert len(expired) == 1
    assert expired[0]["status"] == "delivered"
    assert len(seen) == 1


async def test_concurrent_resubmit_executes_once(client, monkeypatch):
    calls, access, agent_id = await _deferred(client, monkeypatch)
    pending = await _request(client, access, agent_id, {"amount": 5})
    approval_id = pending["error"]["data"]["approval_id"]
    assert (await client.post(f"/approvals/{approval_id}/approve")).status_code == 200

    async def resubmit():
        return await _request(client, access, agent_id, {"amount": 5}, run_id=pending["error"]["data"]["saturnRunId"])

    results = await asyncio.gather(resubmit(), resubmit(), return_exceptions=True)
    assert calls["n"] == 1
    successes = [item for item in results if isinstance(item, dict) and "result" in item]
    assert len(successes) >= 1


async def test_free_plan_manages_webhooks(client):
    created = await client.post(
        "/admin/orgs",
        json={
            "name": "Free Hooks",
            "admin_email": "free-hooks@example.com",
            "admin_name": "Free Hooks",
            "admin_password": "password1",
            "plan": "free",
        },
    )
    assert created.status_code == 201, created.text
    signed = await client.post("/auth/login", json={"email": "free-hooks@example.com", "password": "password1"})
    assert signed.status_code == 200, signed.text
    headers = {"Authorization": f"Bearer {signed.json()['token']}"}
    from tests.totp_access import finish_totp

    await finish_totp(client, headers)
    made = await client.post(
        "/org/webhooks",
        headers=headers,
        json={"url": "https://hooks.example.com/saturn", "events": ["approval.granted"]},
    )
    assert made.status_code == 201, made.text
    listed = await client.get("/org/webhooks", headers=headers)
    assert listed.status_code == 200
    assert [item["url"] for item in listed.json()] == ["https://hooks.example.com/saturn"]
    window = await client.get("/org/approval-window", headers=headers)
    assert window.status_code == 200, window.text

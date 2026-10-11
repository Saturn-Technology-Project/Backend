async def test_profile_notices_email_only_the_selected_events(client, monkeypatch):
    sent: list[tuple] = []

    async def capture(email, *, kind, tool, agent, link, locale=None, lines=(), detail=""):
        sent.append((email, kind, tool, locale, detail, link))
        return True

    monkeypatch.setattr("app.features.identity.auth.services.notices.send_notice", capture)

    current = await client.get("/auth/notices")
    assert current.status_code == 200
    assert current.json() == {"approval": False, "execute": False, "audit": False}

    saved = await client.patch("/auth/notices", json={"approval": True, "execute": False, "audit": False})
    assert saved.status_code == 200
    assert saved.json()["approval"] is True

    created = await client.post(
        "/runs",
        json={"agent_id": "agent_customer_ops", "input": "Refund Acme's duplicate charge."},
    )
    assert created.status_code == 201, created.text
    assert sent
    assert {item[1] for item in sent} == {"approval"}
    assert sent[0][0] == "rick@saturn.ai"
    assert "billing.refund" in {item[2] for item in sent}
    assert "/approvals" in sent[0][5]
    assert "sk-" not in " ".join(sent[0])


def _capture(sent: list):
    async def capture(email, *, kind, tool, agent, link, locale=None, lines=(), detail=""):
        sent.append({"email": email, "kind": kind, "tool": tool, "detail": detail, "lines": lines, "link": link})
        return True

    return capture


async def test_one_task_with_several_tools_sends_one_completion_email(client, monkeypatch):
    from app.features.execution.activity.services.audit_service import audit_service

    sent: list[dict] = []
    audits: list[str] = []
    original = audit_service.log

    async def record(*args, **kwargs):
        event = kwargs.get("event")
        audits.append(getattr(event, "value", event))
        return await original(*args, **kwargs)

    monkeypatch.setattr("app.features.identity.auth.services.notices.send_notice", _capture(sent))
    monkeypatch.setattr(audit_service, "log", record)
    saved = await client.patch("/auth/notices", json={"approval": False, "execute": True, "audit": True})
    assert saved.status_code == 200
    created = await client.post(
        "/runs",
        json={"agent_id": "agent_customer_ops", "input": "Refund 50 for Acme"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "completed"
    assert audits.count("tool_executed") == 3
    assert audits.count("tool_requested") == 3
    assert [item["kind"] for item in sent] == ["completed"]
    assert sent[0]["tool"] == "Refund 50 for Acme"
    assert any(line.startswith("Tools") or line.startswith("工具") for line in sent[0]["lines"])
    replay = await client.post(f"/runs/{created.json()['id']}/notice", json={"status": "completed"})
    assert replay.status_code == 204, replay.text
    again = await client.post(f"/runs/{created.json()['id']}/notice", json={"status": "completed"})
    assert again.status_code == 204, again.text
    assert [item["kind"] for item in sent] == ["completed"]


async def test_a_failed_task_sends_one_failure_summary_and_a_security_alert(client, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr("app.features.identity.auth.services.notices.send_notice", _capture(sent))
    saved = await client.patch("/auth/notices", json={"approval": False, "execute": True, "audit": True})
    assert saved.status_code == 200
    created = await client.post(
        "/runs",
        json={"agent_id": "agent_customer_ops", "input": "Refund 5000 for Acme"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "failed"
    assert [item["kind"] for item in sent] == ["security", "failed"]
    assert "billing.refund" in sent[0]["tool"]
    assert "known" in sent[1]["detail"].lower() or "確定" in sent[1]["detail"]
    assert "completed" not in {item["kind"] for item in sent}


async def test_waiting_for_approval_does_not_send_a_completion_email(client, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr("app.features.identity.auth.services.notices.send_notice", _capture(sent))
    saved = await client.patch("/auth/notices", json={"approval": True, "execute": True, "audit": True})
    assert saved.status_code == 200
    created = await client.post(
        "/runs",
        json={"agent_id": "agent_customer_ops", "input": "Refund Acme's duplicate charge."},
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "waiting_approval"
    assert [item["kind"] for item in sent] == ["approval"]
    early = await client.post(f"/runs/{created.json()['id']}/notice", json={"status": "completed"})
    assert early.status_code == 409, early.text
    assert [item["kind"] for item in sent] == ["approval"]
    approved = await client.post(f"/approvals/{created.json()['approval_id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "completed"
    assert [item["kind"] for item in sent] == ["approval", "completed"]


async def test_email_delivery_failure_leaves_the_run_and_its_audit(client, monkeypatch):
    from app.features.execution.activity.services.audit_service import audit_service

    audits: list[str] = []
    original = audit_service.log

    async def record(*args, **kwargs):
        event = kwargs.get("event")
        audits.append(getattr(event, "value", event))
        return await original(*args, **kwargs)

    async def broken(*args, **kwargs):
        raise RuntimeError("mailbox down")

    monkeypatch.setattr(audit_service, "log", record)
    monkeypatch.setattr("app.features.identity.auth.services.notices.send_notice", broken)
    saved = await client.patch("/auth/notices", json={"approval": False, "execute": True, "audit": False})
    assert saved.status_code == 200
    created = await client.post(
        "/runs",
        json={"agent_id": "agent_customer_ops", "input": "Refund 50 for Acme"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "completed"
    assert audits.count("tool_executed") == 3
    assert "run_completed" in audits


def test_task_notice_does_not_include_secrets():
    from app.features.identity.auth.services.mailer import _notice_message
    from app.features.identity.auth.services.notices import _public_params, _public_text

    hidden = _public_params({"api_key": "sk_live_secret", "amount": 50, "note": "ordinary"})
    assert "sk_live_secret" not in hidden
    assert "••••" in hidden
    assert "50" in hidden
    assert _public_text("Bearer sk_live_secret", 80) == ""
    _subject, text, html = _notice_message(
        "completed",
        "Refund 50 for Acme",
        "Acme Ops",
        "https://dashboard.saturn-core.com/activity?event=run_1",
        "en-US",
        lines=("Tools · 3",),
        detail=hidden,
    )
    assert "sk_live_secret" not in text
    assert "sk_live_secret" not in html
    assert "Task completed" in text
    assert "https://dashboard.saturn-core.com/activity?event=run_1" in html

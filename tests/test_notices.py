async def test_profile_notices_email_only_the_selected_events(client, monkeypatch):
    sent: list[tuple[str, str]] = []

    async def capture(email, *, kind, tool, agent, link, locale=None):
        sent.append((email, kind, tool, locale))

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

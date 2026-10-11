import pytest


@pytest.mark.asyncio
async def test_signup_rejects_a_wrong_code(client, monkeypatch):
    sent = {}

    async def capture(_email, code, _locale=None):
        sent["code"] = code

    monkeypatch.setattr("app.features.identity.auth.controllers.auth.send_signup_code", capture)
    payload = {"name": "Nia", "email": "nia@personal.test", "password": "password1"}
    requested = await client.post("/auth/register/code", json=payload)
    assert requested.status_code == 204, requested.text
    wrong = "111111" if sent["code"] == "000000" else "000000"
    rejected = await client.post("/auth/register", json={**payload, "code": wrong})
    assert rejected.status_code == 400
    signed = await client.post("/auth/login", json={"email": payload["email"], "password": payload["password"]})
    assert signed.status_code == 401


@pytest.mark.asyncio
async def test_email_second_factor_completes_login(client, monkeypatch):
    sent = {}

    async def capture(_email, code, _locale=None):
        sent["code"] = code

    monkeypatch.setattr("app.features.identity.auth.controllers.auth.send_login_code", capture)
    requested = await client.post("/auth/mfa/email/code")
    assert requested.status_code == 204, requested.text
    enabled = await client.post("/auth/mfa/email", json={"code": sent["code"]})
    assert enabled.status_code == 204, enabled.text
    status = await client.get("/auth/mfa")
    assert status.json()["email_mfa"] is True

    blocked = await client.post(
        "/auth/login",
        json={"email": "rick@saturn.ai", "password": "saturn"},
    )
    assert blocked.status_code == 200, blocked.text
    body = blocked.json()
    assert body["mfa_required"] is True
    assert body["token"] is None
    assert "email" in body["methods"]
    wrong = "111111" if sent["code"] == "000000" else "000000"
    rejected = await client.post(
        "/auth/login/email",
        json={"challenge": body["challenge"], "code": wrong},
    )
    assert rejected.status_code == 400

    signed = await client.post(
        "/auth/login/email",
        json={"challenge": body["challenge"], "code": sent["code"]},
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["token"]
    me = await client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {signed.json()['token']}"},
    )
    assert me.status_code == 200

    disabled = await client.delete("/auth/mfa/email")
    assert disabled.status_code == 204
    plain = await client.post(
        "/auth/login",
        json={"email": "rick@saturn.ai", "password": "saturn"},
    )
    assert plain.status_code == 200, plain.text
    assert plain.json()["token"]


@pytest.mark.asyncio
async def test_passkey_options_follow_the_browser_origin(client):
    missing = await client.post("/auth/passkeys/options")
    assert missing.status_code == 400
    options = await client.post(
        "/auth/passkeys/options",
        headers={"Origin": "http://localhost:3000"},
    )
    assert options.status_code == 200, options.text
    body = options.json()
    assert body["rp"]["id"] == "localhost"
    assert body["rp"]["name"] == "Saturn"
    assert body["challenge"]
    rejected = await client.post(
        "/auth/passkeys",
        headers={"Origin": "http://localhost:3000"},
        json={"credential": {"id": "not-a-passkey", "type": "public-key", "response": {}}},
    )
    assert rejected.status_code == 400

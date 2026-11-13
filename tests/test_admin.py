async def test_seed_user_is_platform_admin(client):
    me = (await client.get("/auth/me")).json()
    assert me["is_platform_admin"] is True
    stats = await client.get("/admin/stats")
    assert stats.status_code == 200, stats.text
    body = stats.json()
    assert body["organizations"] >= 1
    assert body["users"] >= 1
    assert body["agents"] >= 1


async def test_non_admin_cannot_access_admin(client):
    invited = await client.post(
        "/org/invites",
        json={"email": "viewer@saturn.ai", "name": "Viewer", "role": "viewer"},
    )
    assert invited.status_code == 201, invited.text
    accepted = await client.post(
        "/auth/accept-invite",
        json={"token": invited.json()["token"], "password": "password1"},
    )
    assert accepted.status_code == 200, accepted.text
    headers = {"Authorization": f"Bearer {accepted.json()['token']}"}
    assert (await client.get("/admin/stats", headers=headers)).status_code == 403
    assert (await client.get("/admin/orgs", headers=headers)).status_code == 403


async def test_onboard_org_and_manage_members(client):
    created = await client.post(
        "/admin/orgs",
        json={
            "name": "Acme",
            "admin_email": "admin@acme.test",
            "admin_name": "Acme Admin",
            "admin_password": "password1",
            "admin_title": "Founder",
            "plan": "enterprise",
        },
    )
    assert created.status_code == 201, created.text
    org = created.json()
    assert org["name"] == "Acme"
    assert org["user_count"] == 1
    assert org["agent_count"] == 0
    assert org["runtime_status"] == "disconnected"
    assert org["mcp_status"] == "none"
    assert org["last_heartbeat_at"] is None
    assert org["health"]["runtime"] == "disconnected"
    assert org["health"]["mcp"] == "none"
    assert org["health"]["agents"] == "none"
    assert org["health"]["policies"] == "ok"
    assert "Connector not connected" in org["health"]["issues"]
    assert org["members"][0]["email"] == "admin@acme.test"
    assert org["members"][0]["role"] == "admin"

    runtimes = (await client.get("/admin/runtimes")).json()
    assert all(item["id"] != f"runtime_{org['id']}" for item in runtimes)
    assert all(item["organization_id"] != org["id"] for item in runtimes)

    listed = (await client.get("/admin/orgs")).json()
    assert any(item["id"] == org["id"] for item in listed)

    detail = await client.get(f"/admin/orgs/{org['id']}")
    assert detail.status_code == 200
    assert len(detail.json()["members"]) == 1

    member = await client.post(
        f"/admin/orgs/{org['id']}/members",
        json={
            "email": "ops@acme.test",
            "name": "Ops",
            "password": "password1",
            "role": "operator",
        },
    )
    assert member.status_code == 201, member.text
    assert member.json()["role"] == "operator"

    updated = await client.patch(
        f"/admin/orgs/{org['id']}/members/{member.json()['id']}",
        json={"status": "disabled"},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "disabled"

    users = (await client.get("/admin/users")).json()
    emails = {item["email"] for item in users}
    assert "admin@acme.test" in emails
    assert "ops@acme.test" in emails

    renamed = await client.patch(f"/admin/orgs/{org['id']}", json={"name": "Acme Corp"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Acme Corp"

    stats = (await client.get("/admin/stats")).json()
    assert stats["organizations"] >= 2
    assert stats["users"] >= 2


async def test_platform_overview_and_org_lifecycle(client):
    overview = await client.get("/admin/overview")
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert "organizations" in body["stats"]
    assert {item["id"] for item in body["services"]} >= {"api", "gateway", "database"}

    created = await client.post(
        "/admin/orgs",
        json={
            "name": "Prism",
            "admin_email": "ops@prism.test",
            "admin_name": "Prism Ops",
            "admin_password": "password1",
            "plan": "free",
        },
    )
    assert created.status_code == 201, created.text
    org = created.json()
    assert org["plan"] == "free"
    assert org["kind"] == "personal"
    assert org["status"] == "active"
    assert "health" in org

    patched = await client.patch(
        f"/admin/orgs/{org['id']}",
        json={"status": "suspended", "plan": "enterprise"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "suspended"
    assert patched.json()["plan"] == "enterprise"
    assert patched.json()["kind"] == "organization"

    assert (await client.get("/admin/connections")).status_code == 200
    assert (await client.get("/admin/runtimes")).status_code == 200
    assert (await client.get("/admin/mcp")).status_code == 200
    assert (await client.get("/admin/usage")).status_code == 200
    audit = await client.get("/admin/audit")
    assert audit.status_code == 200
    events = {item["event"] for item in audit.json()}
    assert "org_onboarded" in events
    assert "org_updated" in events
    settings = await client.get("/admin/settings")
    assert settings.status_code == 200
    assert settings.json()["platform"]["name"] == "Saturn"


def capture_reset_codes(monkeypatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []

    async def capture(email, code, _locale=None):
        sent.append((email, code))

    monkeypatch.setattr("app.shared.runtime.email_configured", lambda: True)
    monkeypatch.setattr("app.features.identity.auth.services.mailer.send_reset_code", capture)
    return sent


async def test_admin_can_reset_member_password(client, monkeypatch):
    created = await client.post(
        "/admin/orgs",
        json={
            "name": "Acme",
            "admin_email": "admin@acme.test",
            "admin_name": "Acme Admin",
            "admin_password": "password1",
        },
    )
    assert created.status_code == 201, created.text
    org = created.json()
    member = org["members"][0]

    login = await client.post(
        "/auth/login",
        json={"email": "admin@acme.test", "password": "password1"},
    )
    assert login.status_code == 200, login.text
    old_headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert (await client.get("/auth/me", headers=old_headers)).status_code == 200

    # Without email delivery the reset is refused and nothing changes.
    unconfigured = await client.post(
        f"/admin/orgs/{org['id']}/members/{member['id']}/password",
        json={"password": "newpass12"},
    )
    assert unconfigured.status_code == 503
    assert (await client.get("/auth/me", headers=old_headers)).status_code == 200

    sent = capture_reset_codes(monkeypatch)
    # The admin's chosen password is ignored: the user gets a one-time code by email instead.
    reset = await client.post(
        f"/admin/orgs/{org['id']}/members/{member['id']}/password",
        json={"password": "newpass12"},
    )
    assert reset.status_code == 200, reset.text
    assert [email for email, _code in sent] == ["admin@acme.test"]

    assert (await client.get("/auth/me", headers=old_headers)).status_code == 401
    for password in ("password1", "newpass12"):
        assert (
            await client.post(
                "/auth/login",
                json={"email": "admin@acme.test", "password": password},
            )
        ).status_code == 401
    chosen = await client.post(
        "/auth/reset-password",
        json={"email": "admin@acme.test", "code": sent[0][1], "password": "chosen-by-user"},
    )
    assert chosen.status_code == 204, chosen.text
    again = await client.post(
        "/auth/login",
        json={"email": "admin@acme.test", "password": "chosen-by-user"},
    )
    assert again.status_code == 200, again.text


async def test_onboard_rejects_duplicate_email(client):
    response = await client.post(
        "/admin/orgs",
        json={
            "name": "Dup",
            "admin_email": "rick@saturn.ai",
            "admin_name": "Rick",
            "admin_password": "password1",
        },
    )
    assert response.status_code == 409


async def test_a_suspended_organization_is_refused_everywhere(client):
    from tests.oauth_access import oauth_access
    from tests.totp_access import finish_totp

    created = await client.post(
        "/admin/orgs",
        json={
            "name": "Halted",
            "admin_email": "owner@halted.test",
            "admin_name": "Owner",
            "admin_password": "password1",
            "plan": "enterprise",
        },
    )
    assert created.status_code == 201, created.text
    org_id = created.json()["id"]
    login = await client.post("/auth/login", json={"email": "owner@halted.test", "password": "password1"})
    assert login.status_code == 200, login.text
    member = {"Authorization": f"Bearer {login.json()['token']}"}
    # An organization admin needs a second factor before the console answers.
    await finish_totp(client, member)
    assert (await client.get("/agents", headers=member)).status_code == 200
    access = await oauth_access(client, "Halted Agent", email="owner@halted.test", password="password1")
    agent = {"Authorization": f"Bearer {access}"}
    opened = await client.post("/mcp", headers=agent, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert opened.status_code == 200, opened.text

    suspended = await client.patch(f"/admin/orgs/{org_id}", json={"status": "suspended"})
    assert suspended.status_code == 200, suspended.text

    for path in ("/agents", "/org", "/policies", "/approvals"):
        refused = await client.get(path, headers=member)
        assert refused.status_code == 403, (path, refused.text)
        assert refused.json()["detail"] == "Organization is suspended"
    # The member can still see who they are and sign out.
    assert (await client.get("/auth/me", headers=member)).status_code == 200
    call = await client.post("/mcp", headers=agent, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert call.status_code == 403, call.text
    # The platform admin who suspended it is not locked out of anything.
    assert (await client.get("/admin/orgs")).status_code == 200
    assert (await client.get("/agents")).status_code == 200

    restored = await client.patch(f"/admin/orgs/{org_id}", json={"status": "active"})
    assert restored.status_code == 200, restored.text
    assert (await client.get("/agents", headers=member)).status_code == 200
    call = await client.post("/mcp", headers=agent, json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert call.status_code == 200, call.text


async def test_audit_filters_and_pages(client):
    orgs = []
    for name, email in (("Halo", "ops@halo.test"), ("Rings", "ops@rings.test")):
        created = await client.post(
            "/admin/orgs",
            json={"name": name, "admin_email": email, "admin_name": name, "admin_password": "password1"},
        )
        assert created.status_code == 201, created.text
        orgs.append(created.json())
    halo, rings = orgs
    patched = await client.patch(f"/admin/orgs/{halo['id']}", json={"status": "suspended"})
    assert patched.status_code == 200, patched.text

    everything = await client.get("/admin/audit")
    assert everything.status_code == 200, everything.text
    total = int(everything.headers["X-Total-Count"])
    assert total == len(everything.json()) >= 3

    for_halo = await client.get("/admin/audit", params={"organization_id": halo["id"]})
    assert {item["organization_id"] for item in for_halo.json()} == {halo["id"]}
    assert {item["event"] for item in for_halo.json()} == {"org_onboarded", "org_updated"}
    assert int(for_halo.headers["X-Total-Count"]) == len(for_halo.json())

    onboarded = await client.get("/admin/audit", params={"event": "org_onboarded"})
    assert {item["event"] for item in onboarded.json()} == {"org_onboarded"}
    assert {halo["id"], rings["id"]} <= {item["organization_id"] for item in onboarded.json()}

    both = await client.get(
        "/admin/audit", params={"organization_id": rings["id"], "event": "org_updated"}
    )
    assert both.json() == []
    assert both.headers["X-Total-Count"] == "0"

    first = await client.get("/admin/audit", params={"limit": 2})
    second = await client.get("/admin/audit", params={"limit": 2, "offset": 2})
    assert len(first.json()) == 2
    assert int(first.headers["X-Total-Count"]) == total
    assert [item["id"] for item in first.json() + second.json()] == [
        item["id"] for item in everything.json()[:4]
    ]

    assert (await client.get("/admin/audit", params={"since": "2999-01-01T00:00:00Z"})).json() == []
    recent = await client.get("/admin/audit", params={"since": "2000-01-01T08:00:00+08:00"})
    assert int(recent.headers["X-Total-Count"]) == total

    names = await client.get("/admin/audit/events")
    assert names.status_code == 200, names.text
    assert names.json() == sorted(names.json())
    assert {"org_onboarded", "org_updated"} <= set(names.json())


async def test_usage_counts_only_the_requested_range(client):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from app.db.database import get_session
    from app.main import app
    from app.models import Action, Run

    created = await client.post(
        "/runs",
        json={"agent_id": "agent_customer_ops", "input": "Refund Acme's duplicate charge."},
    )
    assert created.status_code == 201, created.text
    run = created.json()

    everyone = (await client.get("/admin/usage")).json()
    organization_id = next(item["organization_id"] for item in everyone if item["runs"])

    def row(response):
        assert response.status_code == 200, response.text
        return next(item for item in response.json() if item["organization_id"] == organization_id)

    all_time = row(await client.get("/admin/usage"))
    assert all_time["runs"] == 1
    assert all_time["tool_calls"] == len(run["actions"]) >= 1
    assert row(await client.get("/admin/usage", params={"days": 7})) == all_time

    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    session_gen = app.dependency_overrides[get_session]()
    session = await session_gen.__anext__()
    try:
        await session.execute(update(Run).where(Run.id == run["id"]).values(created_at=long_ago))
        await session.execute(
            update(Action).where(Action.run_id == run["id"]).values(created_at=long_ago)
        )
        await session.commit()
    finally:
        await session_gen.aclose()

    last_week = row(await client.get("/admin/usage", params={"days": 7}))
    assert (last_week["runs"], last_week["tool_calls"]) == (0, 0)
    assert row(await client.get("/admin/usage", params={"days": 90})) == all_time
    assert row(await client.get("/admin/usage")) == all_time

    assert (await client.get("/admin/usage", params={"days": 0})).status_code == 422
    assert (await client.get("/admin/usage", params={"days": 9999})).status_code == 422

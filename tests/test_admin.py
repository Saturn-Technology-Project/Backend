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


async def test_admin_can_reset_member_password(client):
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

    too_short = await client.post(
        f"/admin/orgs/{org['id']}/members/{member['id']}/password",
        json={"password": "short"},
    )
    assert too_short.status_code == 400

    reset = await client.post(
        f"/admin/orgs/{org['id']}/members/{member['id']}/password",
        json={"password": "newpass12"},
    )
    assert reset.status_code == 200, reset.text

    assert (await client.get("/auth/me", headers=old_headers)).status_code == 401
    assert (
        await client.post(
            "/auth/login",
            json={"email": "admin@acme.test", "password": "password1"},
        )
    ).status_code == 401
    again = await client.post(
        "/auth/login",
        json={"email": "admin@acme.test", "password": "newpass12"},
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

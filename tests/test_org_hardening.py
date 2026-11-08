"""Regression tests for invite delivery, admin passwords, billing access and tenant scoping."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.db.database import get_session
from app.main import app
from app.models import Invitation, Organization, Tool


async def _with_session(fn):
    gen = app.dependency_overrides[get_session]()
    session = await gen.__anext__()
    try:
        result = await fn(session)
        await session.commit()
        return result
    finally:
        await gen.aclose()


def _production(monkeypatch, *, email: bool = True) -> dict[str, list]:
    """Behave as production does: no dev shortcuts, real email delivery (captured here)."""
    sent: dict[str, list] = {"invites": [], "resets": []}

    async def invite(email_address, token, **kwargs):
        sent["invites"].append((email_address, token, kwargs))

    async def reset(email_address, code, _locale=None):
        sent["resets"].append((email_address, code))

    monkeypatch.setattr("app.shared.runtime.local_dev_mode", lambda: False)
    monkeypatch.setattr("app.shared.runtime.email_configured", lambda: email)
    monkeypatch.setattr("app.features.identity.auth.services.mailer.send_invite", invite)
    monkeypatch.setattr("app.features.identity.auth.services.mailer.send_reset_code", reset)
    return sent


async def _member(client, email: str, role: str = "viewer", **extra) -> dict[str, str]:
    invited = await client.post("/org/invites", json={"email": email, "name": email.split("@")[0], "role": role, **extra})
    assert invited.status_code == 201, invited.text
    accepted = await client.post(
        "/auth/accept-invite",
        json={"token": invited.json()["token"], "password": "password1"},
    )
    assert accepted.status_code == 200, accepted.text
    return {"Authorization": f"Bearer {accepted.json()['token']}"}


def test_local_dev_mode_requires_in_process_runtime_and_no_cloud_run(monkeypatch):
    from app.shared import runtime

    monkeypatch.delenv("K_SERVICE", raising=False)
    assert runtime.local_dev_mode() is True  # tests run with IN_PROCESS_RUNTIME=true
    monkeypatch.setenv("K_SERVICE", "saturn-api")
    assert runtime.local_dev_mode() is False


def test_invite_link_points_at_console_accept_page():
    from app.features.identity.auth.services.mailer import _invite_message, invite_link

    link = invite_link("inv_abc+/=")
    assert link.endswith("/accept-invite?token=inv_abc%2B%2F%3D")
    subject, text, html = _invite_message("inv_abc", "Acme", "Rick", 7, "en-US")
    assert "Acme" in subject
    assert link.split("?")[0] in text
    assert "inv_abc" in html


# M-05


async def test_production_invite_token_goes_only_to_email(client, monkeypatch):
    sent = _production(monkeypatch)
    created = await client.post(
        "/org/invites",
        json={"email": "New.Person@Example.com", "name": "New", "role": "viewer"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["token"] is None
    assert len(sent["invites"]) == 1
    email, token, details = sent["invites"][0]
    assert email == "new.person@example.com"
    assert token.startswith("inv_")
    assert details["organization"]
    # The emailed token is the one that works.
    accepted = await client.post("/auth/accept-invite", json={"token": token, "password": "password1"})
    assert accepted.status_code == 200, accepted.text


async def test_production_invite_without_email_is_refused_and_not_stored(client, monkeypatch):
    _production(monkeypatch, email=False)
    refused = await client.post(
        "/org/invites",
        json={"email": "nomail@example.com", "name": "No mail", "role": "viewer"},
    )
    assert refused.status_code == 503
    invites = (await client.get("/org/invites")).json()
    assert all(item["email"] != "nomail@example.com" for item in invites)


async def test_failed_invite_email_leaves_no_invite(client, monkeypatch):
    _production(monkeypatch)

    async def broken(*_args, **_kwargs):
        raise HTTPException(502, "Could not send email")

    monkeypatch.setattr("app.features.identity.auth.services.mailer.send_invite", broken)
    failed = await client.post(
        "/org/invites",
        json={"email": "bounce@example.com", "name": "Bounce", "role": "viewer"},
    )
    assert failed.status_code == 502
    invites = (await client.get("/org/invites")).json()
    assert all(item["email"] != "bounce@example.com" for item in invites)


async def test_invites_do_not_reveal_accounts_in_other_orgs(client, monkeypatch):
    onboarded = await client.post(
        "/admin/orgs",
        json={
            "name": "Elsewhere",
            "admin_email": "cfo@victim.test",
            "admin_name": "CFO",
            "admin_password": "password1",
            "plan": "team",
        },
    )
    assert onboarded.status_code == 201, onboarded.text
    sent = _production(monkeypatch)

    taken = await client.post("/org/invites", json={"email": "cfo@victim.test", "name": "CFO", "role": "viewer"})
    fresh = await client.post("/org/invites", json={"email": "nobody@victim.test", "name": "Nobody", "role": "viewer"})
    assert taken.status_code == fresh.status_code == 201
    assert set(taken.json()) == set(fresh.json())
    assert taken.json()["token"] is None and fresh.json()["token"] is None
    # Only the address without an account is emailed; nothing reaches the other org's user.
    assert [item[0] for item in sent["invites"]] == ["nobody@victim.test"]

    member = await client.post("/org/invites", json={"email": "rick@saturn.ai", "name": "Rick", "role": "viewer"})
    pending = await client.post("/org/invites", json={"email": "nobody@victim.test", "name": "Again", "role": "viewer"})
    assert member.status_code == pending.status_code == 409
    assert member.json()["detail"] == pending.json()["detail"]
    assert "another org" not in member.json()["detail"]


async def test_dev_mode_still_returns_the_invite_token(client):
    created = await client.post("/org/invites", json={"email": "dev@example.com", "name": "Dev", "role": "viewer"})
    assert created.status_code == 201, created.text
    assert created.json()["token"].startswith("inv_")


async def test_accepting_an_invite_for_an_existing_account_fails(client):
    onboarded = await client.post(
        "/admin/orgs",
        json={"name": "Owner Co", "admin_email": "owner@elsewhere.test", "admin_name": "Owner",
              "admin_password": "password1", "plan": "team"},
    )
    assert onboarded.status_code == 201, onboarded.text
    # Dev mode returns the token, so the inviter could try to take over the address. It fails.
    created = await client.post("/org/invites", json={"email": "owner@elsewhere.test", "name": "x", "role": "viewer"})
    assert created.status_code == 201, created.text
    taken = await client.post(
        "/auth/accept-invite", json={"token": created.json()["token"], "password": "password1"}
    )
    assert taken.status_code == 409
    login = await client.post("/auth/login", json={"email": "owner@elsewhere.test", "password": "password1"})
    assert login.status_code == 200


# L-23


async def test_production_onboarding_never_uses_the_admin_chosen_password(client, monkeypatch):
    sent = _production(monkeypatch)
    created = await client.post(
        "/admin/orgs",
        json={
            "name": "Prod Co",
            "admin_email": "owner@prod.test",
            "admin_name": "Owner",
            "admin_password": "known-by-platform-admin",
            "plan": "team",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["members"][0]["email"] == "owner@prod.test"
    login = await client.post(
        "/auth/login", json={"email": "owner@prod.test", "password": "known-by-platform-admin"}
    )
    assert login.status_code == 401
    assert [email for email, _code in sent["resets"]] == ["owner@prod.test"]
    chosen = await client.post(
        "/auth/reset-password",
        json={"email": "owner@prod.test", "code": sent["resets"][0][1], "password": "owner-chosen"},
    )
    assert chosen.status_code == 204, chosen.text
    assert (
        await client.post("/auth/login", json={"email": "owner@prod.test", "password": "owner-chosen"})
    ).status_code == 200

    added = await client.post(
        f"/admin/orgs/{created.json()['id']}/members",
        json={"email": "ops@prod.test", "name": "Ops", "password": "also-known", "role": "operator"},
    )
    assert added.status_code == 201, added.text
    assert (await client.post("/auth/login", json={"email": "ops@prod.test", "password": "also-known"})).status_code == 401
    assert sent["resets"][-1][0] == "ops@prod.test"


async def test_production_onboarding_without_email_creates_nothing(client, monkeypatch):
    before = len((await client.get("/admin/orgs")).json())
    _production(monkeypatch, email=False)
    refused = await client.post(
        "/admin/orgs",
        json={"name": "No Mail Co", "admin_email": "owner@nomail.test", "admin_name": "Owner", "plan": "team"},
    )
    assert refused.status_code == 503
    assert len((await client.get("/admin/orgs")).json()) == before
    users = (await client.get("/admin/users")).json()
    assert all(item["email"] != "owner@nomail.test" for item in users)


async def test_dev_admin_passwords_keep_the_minimum_length(client):
    short = await client.post(
        "/admin/orgs",
        json={"name": "Short", "admin_email": "s@short.test", "admin_name": "S", "admin_password": "short", "plan": "team"},
    )
    assert short.status_code == 400


# M-27


async def test_subscription_and_pay_require_team_write(client):
    viewer = await _member(client, "billing-viewer@saturn.ai")
    assert (await client.get("/org/subscription", headers=viewer)).status_code == 403
    assert (await client.post("/org/subscription/invoices/inv_x/pay", headers=viewer)).status_code == 403


async def test_pay_refuses_amounts_without_a_payment_processor(client):
    async def stage(session):
        org = await session.get(Organization, "org_saturn")
        org.billing = {
            "payment": {"brand": "visa", "last4": "4242", "exp": "12/30"},
            "invoices": [
                {"id": "inv_due", "number": "INV-1", "period": "2026-10", "issued_at": "2026-10-01T00:00:00+00:00",
                 "amount": "49.00", "status": "open", "lines": []},
                {"id": "inv_zero", "number": "INV-2", "period": "2026-09", "issued_at": "2026-09-01T00:00:00+00:00",
                 "amount": "0.00", "status": "open", "lines": []},
            ],
        }

    await _with_session(stage)
    due = await client.post("/org/subscription/invoices/inv_due/pay")
    assert due.status_code == 501
    assert due.json()["detail"] == "Payment processor is not configured"
    invoices = {item["id"]: item for item in (await client.get("/org/subscription")).json()["invoices"]}
    assert invoices["inv_due"]["status"] == "open"
    zero = await client.post("/org/subscription/invoices/inv_zero/pay")
    assert zero.status_code == 200, zero.text
    assert {item["id"]: item["status"] for item in zero.json()["invoices"]}["inv_zero"] == "paid"


# L-18


async def test_agent_ids_are_generated_by_the_server(client):
    probe = await client.post("/agents", json={"id": "agent_customer_ops", "name": "Probe"})
    assert probe.status_code == 201, probe.text
    agent_id = probe.json()["id"]
    assert agent_id != "agent_customer_ops"
    assert agent_id.startswith("agent_") and len(agent_id) == len("agent_") + 32
    named = await client.post("/agents", json={"name": "Customer Ops"})
    assert named.json()["id"] != "agent_customer_ops"
    assert not named.json()["id"].startswith("agent_customer")


# L-19


async def test_tool_name_lookup_stays_in_the_callers_org(client):
    async def stage(session):
        session.add(Organization(id="org_tools_other", name="Other", plan="team", kind="organization"))
        await session.flush()
        session.add(Tool(id="tool_other_refund", organization_id="org_tools_other", name="billing.refund",
                         description="", type="mcp", status="active"))
        session.add(Tool(id="tool_other_only", organization_id="org_tools_other", name="other.only",
                         description="", type="mcp", status="active"))

    await _with_session(stage)
    mine = await client.get("/tools/billing.refund")
    assert mine.status_code == 200, mine.text
    assert mine.json()["id"] != "tool_other_refund"
    assert (await client.get("/tools/other.only")).status_code == 404
    assert (await client.get("/tools/tool_other_only")).status_code == 404


# L-20


async def test_request_size_limits(client):
    assert (await client.post("/agents", json={"name": "x" * 256})).status_code == 422
    agent_id = (await client.post("/agents", json={"name": "Sized"})).json()["id"]
    huge = {"instructions": "x" * (70 * 1024)}
    assert (await client.patch(f"/agents/{agent_id}", json={"settings": huge})).status_code == 422
    assert (await client.patch(f"/agents/{agent_id}", json={"model": "m" * 129})).status_code == 422
    assert (await client.patch(f"/agents/{agent_id}", json={"settings": {"instructions": "ok"}})).status_code == 200
    long_env = await client.post("/policies", json={"name": "P", "environment": "e" * 65})
    assert long_env.status_code == 422
    small_value = await client.post(
        "/policies",
        json={
            "name": "P",
            "rules": [{"tool": "billing.refund", "decision": "deny",
                       "conditions": [{"field": "amount", "operator": "eq", "value": "v"}]}],
        },
    )
    assert small_value.status_code != 422, small_value.text
    big_value = await client.post(
        "/policies",
        json={
            "name": "P",
            "rules": [{"tool": "billing.refund", "decision": "deny",
                       "conditions": [{"field": "amount", "operator": "eq", "value": "v" * 20_000}]}],
        },
    )
    assert big_value.status_code == 422


async def test_list_endpoints_page_without_changing_shape(client):
    await _member(client, "page-one@saturn.ai")
    await _member(client, "page-two@saturn.ai")
    everyone = (await client.get("/org/members")).json()
    assert isinstance(everyone, list) and len(everyone) >= 3
    first = (await client.get("/org/members?limit=1")).json()
    second = (await client.get("/org/members?limit=1&offset=1")).json()
    assert len(first) == 1 and len(second) == 1
    assert first[0]["id"] == everyone[0]["id"] and second[0]["id"] == everyone[1]["id"]
    for path in ("/org/members", "/agents", "/tools", "/tool-sets", "/admin/users", "/admin/orgs"):
        assert (await client.get(f"{path}?limit=0")).status_code == 422, path
        assert (await client.get(f"{path}?limit=501")).status_code == 422, path
        page = await client.get(f"{path}?limit=1")
        assert page.status_code == 200, (path, page.text)
        assert isinstance(page.json(), list) and len(page.json()) <= 1


# L-21


async def test_revoked_mcp_servers_stay_revoked(client):
    headers = await _member(client, "revoked@saturn.ai", role="operator")
    me = (await client.get("/org", headers=headers)).json()["me"]
    assert set(me["mcp_server_ids"]) == {"mcp_billing", "mcp_crm", "mcp_email"}
    revoked = await client.patch(f"/org/members/{me['id']}", json={"mcp_server_ids": []})
    assert revoked.status_code == 200, revoked.text
    for _ in range(2):
        again = (await client.get("/org", headers=headers)).json()["me"]
        assert again["mcp_server_ids"] == []
        await client.get("/agents", headers=headers)


# L-22


def test_last_admin_check_locks_the_org_employee_rows():
    from app.features.organization.org.services.member_service import lock_org_employees_statement

    sql = str(lock_org_employees_statement("org_x").compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql
    assert "employees.organization_id" in sql


async def test_demotion_takes_the_lock_before_counting_admins(client, monkeypatch):
    from app.features.organization.org.services.member_service import member_service

    calls: list[str] = []
    original_lock = member_service._lock_org_employees
    original_count = member_service._admin_count

    async def lock(session, organization_id):
        calls.append("lock")
        return await original_lock(session, organization_id)

    async def count(session, organization_id):
        calls.append("count")
        return await original_count(session, organization_id)

    monkeypatch.setattr(member_service, "_lock_org_employees", lock)
    monkeypatch.setattr(member_service, "_admin_count", count)
    members = (await client.get("/org/members")).json()
    admin = next(member for member in members if member["email"] == "rick@saturn.ai")
    refused = await client.patch(f"/org/members/{admin['id']}", json={"role": "viewer"})
    assert refused.status_code == 409
    assert calls == ["lock", "count"]


# I-03


async def test_platform_audit_hides_agent_prompt_text(client):
    agent_id = (await client.post("/agents", json={"name": "Prompted"})).json()["id"]
    secret_prompt = "Internal playbook: never refund ACME more than 500"
    updated = await client.patch(f"/agents/{agent_id}", json={"settings": {"instructions": secret_prompt}})
    assert updated.status_code == 200, updated.text
    audit = (await client.get("/admin/audit")).json()
    event = next(item for item in audit if item["event"] == "prompt_updated")
    assert secret_prompt not in str(event["payload"])
    assert event["payload"]["redacted"] is True
    assert event["payload"]["prompt_length"] == len(secret_prompt)
    assert event["payload"]["agent_id"] == agent_id


async def test_invitation_rows_never_store_the_raw_token(client):
    created = await client.post("/org/invites", json={"email": "hash@example.com", "name": "H", "role": "viewer"})
    token = created.json()["token"]

    async def load(session):
        return (await session.execute(select(Invitation).where(Invitation.email == "hash@example.com"))).scalar_one()

    row = await _with_session(load)
    assert row.token_hash != token and token not in row.token_hash


def test_invite_link_carries_the_invitee_address():
    from app.features.identity.auth.services.mailer import _invite_message, invite_link

    link = invite_link("inv_abc", "New.Person+ops@Example.com")
    assert link.endswith("/accept-invite?token=inv_abc&email=new.person%2Bops@example.com")
    _subject, text, html = _invite_message("inv_abc", "Acme", "", 7, "en-US", "ops@example.com")
    assert "email=ops@example.com" in text
    assert "email=ops@example.com" in html


async def test_platform_agent_views_hide_the_prompt(client):
    agent_id = (await client.post("/agents", json={"name": "Prompted Two"})).json()["id"]
    secret_prompt = "Internal playbook: approve ACME refunds up to 900"
    updated = await client.patch(f"/agents/{agent_id}", json={"settings": {"instructions": secret_prompt}})
    assert updated.status_code == 200, updated.text
    org_id = (await client.get("/org")).json()["organization"]["id"]
    for path in (f"/admin/orgs/{org_id}", f"/admin/orgs/{org_id}/agents"):
        response = await client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert secret_prompt not in response.text, path
    agents = (await client.get(f"/admin/orgs/{org_id}/agents")).json()
    mine = next(item for item in agents if item["id"] == agent_id)
    assert mine["settings"]["instructions_length"] == len(secret_prompt)
    assert "base_url" not in mine["settings"]
    # The tenant's own view still has it.
    assert secret_prompt in (await client.get(f"/agents/{agent_id}")).text

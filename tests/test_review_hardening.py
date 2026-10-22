"""Hardening from the 2026-10-21 security review (L1, L3, L5, L6)."""

from datetime import timedelta

from app.core.ids import utcnow
from app.features.identity.auth.models.throttle import AuthThrottle
from tests.totp_access import finish_totp


async def _session_factory():
    from app.db.database import get_session
    from app.main import app

    return app.dependency_overrides[get_session]


async def test_reset_codes_stop_after_the_daily_cap(client):
    override = await _session_factory()
    gen = override()
    session = await gen.__anext__()
    # Ten earlier requests today, each outside the one-minute resend wait.
    for index in range(10):
        session.add(
            AuthThrottle(
                id=f"thr_test_{index}",
                bucket="email-day:reset:capped@saturn.ai",
                created_at=utcnow() - timedelta(hours=1, minutes=index),
            )
        )
    await session.commit()
    await gen.aclose()
    client.headers.pop("Authorization", None)
    refused = await client.post("/auth/forgot-password", json={"email": "capped@saturn.ai"})
    assert refused.status_code == 429, refused.text


async def test_agent_registrations_do_not_use_up_sign_up(client):
    for index in range(20):
        made = await client.post(
            "/oauth/register",
            json={"client_name": f"Agent {index}", "redirect_uris": ["http://127.0.0.1:8765/callback"]},
        )
        assert made.status_code == 201, made.text
    assert (await client.post("/oauth/register", json={"redirect_uris": ["http://127.0.0.1:1/cb"]})).status_code == 429
    client.headers.pop("Authorization", None)
    signup = await client.post(
        "/auth/register/code",
        json={"name": "New", "email": "fresh-signup@saturn.ai", "password": "password1"},
    )
    assert signup.status_code != 429, signup.text


async def test_org_admin_cannot_disable_a_platform_admin(client):
    invited = await client.post("/org/invites", json={"email": "second-admin@saturn.ai", "name": "Second", "role": "admin"})
    assert invited.status_code == 201, invited.text
    accepted = await client.post("/auth/accept-invite", json={"token": invited.json()["token"], "password": "password1"})
    headers = {"Authorization": f"Bearer {accepted.json()['token']}"}
    await finish_totp(client, headers)
    rick = (await client.get("/auth/me")).json()
    assert rick["is_platform_admin"] is True
    refused = await client.patch(f"/org/members/{rick['employee']['id']}", headers=headers, json={"status": "disabled"})
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "A platform admin cannot be disabled here"


async def test_mcp_sign_in_result_goes_only_to_console_origins(client):
    page = await client.get("/mcp-servers/oauth/callback", params={"state": "unknown", "code": "x"})
    assert page.status_code == 200
    assert '"*"' not in page.text
    assert "https://dashboard.saturn-core.com" in page.text


async def test_oauth_pages_submit_each_form_once(client):
    client.headers.pop("Authorization", None)
    page = await client.get("/oauth/authorize")
    assert "f.dataset.busy==='1'" in page.text
    nonce = page.headers["content-security-policy"].split("'nonce-", 1)[1].split("'", 1)[0]
    assert f'<script nonce="{nonce}">document.addEventListener(\'submit\'' in page.text


async def test_console_session_ends_after_a_day_without_use(client):
    from sqlalchemy import update

    from app.features.identity.auth.models.session import AuthSession

    assert (await client.get("/auth/me")).status_code == 200
    override = await _session_factory()
    gen = override()
    session = await gen.__anext__()
    stale = utcnow() - timedelta(hours=25)
    await session.execute(update(AuthSession).values(last_seen_at=stale, authenticated_at=stale))
    await session.commit()
    await gen.aclose()
    assert (await client.get("/auth/me")).status_code == 401

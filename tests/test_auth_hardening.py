"""Regression tests for the identity, OAuth and core security fixes."""

import re
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.ids import utcnow
from app.features.identity.oauth.services.oauth_service import pkce_s256
from tests.oauth_access import oauth_tokens
from tests.totp_access import finish_totp, next_code


async def _db():
    from app.db.database import get_session
    from app.main import app

    generator = app.dependency_overrides[get_session]()
    session = await anext(generator)
    return session, generator


async def _rick():
    from app.models import User

    session, generator = await _db()
    user = await session.get(User, get_settings().default_actor_id)
    return session, generator, user


async def _enable_real_totp(client) -> str:
    """The test seed marks the admin as enrolled without a secret. Enrol for real."""
    session, generator, user = await _rick()
    try:
        user.totp_enabled = False
        user.totp_secret = ""
        await session.commit()
    finally:
        await generator.aclose()
    return await finish_totp(client, {})


def _no_auth(client):
    saved = client.headers.pop("Authorization", None)
    client.cookies.clear()
    return saved


# H-01: an enrolled authenticator is required at sign-in.


@pytest.mark.asyncio
async def test_totp_is_required_to_sign_in(client):
    secret = await _enable_real_totp(client)
    saved = _no_auth(client)
    try:
        login = await client.post("/auth/login", json={"email": "rick@saturn.ai", "password": "saturn"})
        assert login.status_code == 200, login.text
        body = login.json()
        assert body["mfa_required"] is True
        assert body["token"] is None
        assert body["methods"] == ["totp"]

        wrong = await client.post("/auth/login/totp", json={"challenge": body["challenge"], "code": "000000"})
        assert wrong.status_code == 400

        finished = await client.post(
            "/auth/login/totp", json={"challenge": body["challenge"], "code": next_code(secret)}
        )
        assert finished.status_code == 200, finished.text
        token = finished.json()["token"]
        me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_totp_replaces_the_emailed_code(client):
    from app.features.identity.auth.services.mfa import mfa_service

    secret = await _enable_real_totp(client)
    assert secret
    session, generator, user = await _rick()
    try:
        user.email_mfa = True
        assert await mfa_service.factors(session, user) == ["totp"]
    finally:
        await generator.aclose()


# L-04: a lockout ends.


@pytest.mark.asyncio
async def test_totp_lockout_expires(client):
    from app.features.identity.auth.services.totp import MAX_ATTEMPTS, verify_user_totp
    from fastapi import HTTPException

    secret = await _enable_real_totp(client)
    session, generator, user = await _rick()
    try:
        for _ in range(MAX_ATTEMPTS):
            with pytest.raises(HTTPException):
                await verify_user_totp(session, user, "000000")
        with pytest.raises(HTTPException) as locked:
            await verify_user_totp(session, user, next_code(secret))
        assert locked.value.status_code == 429
        user.totp_locked_until = utcnow() - timedelta(seconds=1)
        await verify_user_totp(session, user, next_code(secret))
        assert user.totp_attempts == 0
    finally:
        await generator.aclose()


@pytest.mark.asyncio
async def test_backup_codes_have_64_bits(client):
    session, generator, user = await _rick()
    try:
        user.totp_enabled = False
        await session.commit()
    finally:
        await generator.aclose()
    setup = await client.post("/auth/totp/setup")
    assert setup.status_code == 200
    assert all(len(code) == 16 for code in setup.json()["backup_codes"])


# H-06: the OAuth page uses the same factors and its session is not an API session.


async def _oauth_login(client, query_name: str = "Scoped Agent"):
    verifier = "d" * 43
    registered = await client.post(
        "/oauth/register",
        json={"client_name": query_name, "redirect_uris": ["http://127.0.0.1:8765/callback"]},
    )
    query = {
        "response_type": "code",
        "client_id": registered.json()["client_id"],
        "redirect_uri": "http://127.0.0.1:8765/callback",
        "code_challenge": pkce_s256(verifier),
        "code_challenge_method": "S256",
        "state": "s",
    }
    signed = await client.post(
        "/oauth/login",
        data={**query, "email": "rick@saturn.ai", "password": "saturn"},
        follow_redirects=False,
    )
    return query, signed


@pytest.mark.asyncio
async def test_oauth_consent_session_is_not_an_api_session(client):
    saved = _no_auth(client)
    try:
        query, signed = await _oauth_login(client)
        assert signed.status_code == 303, signed.text
        cookie = signed.cookies.get("saturn_oauth") or client.cookies.get("saturn_oauth")
        token = cookie.split(".", 1)[0]
        me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 401

        consent = await client.get(signed.headers["location"])
        csrf = re.search(r'name="csrf" value="([^"]+)"', consent.text).group(1)
        allowed = await client.post(
            "/oauth/consent", data={**query, "csrf": csrf, "decision": "allow"}, follow_redirects=False
        )
        assert allowed.status_code == 303
        assert "code" in parse_qs(urlparse(allowed.headers["location"]).query)
        # One consent per sign-in: the consent session is gone.
        client.cookies.set("saturn_oauth", cookie, path="/oauth")
        again = await client.post(
            "/oauth/consent", data={**query, "csrf": csrf, "decision": "allow"}, follow_redirects=False
        )
        assert again.status_code == 400
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_oauth_page_asks_for_totp_not_email(client):
    await _enable_real_totp(client)
    saved = _no_auth(client)
    try:
        _query, signed = await _oauth_login(client, "Totp Agent")
        assert signed.status_code == 200
        assert 'name="method" value="totp"' in signed.text
        assert "emailed" not in signed.text
    finally:
        if saved:
            client.headers["Authorization"] = saved


@pytest.mark.asyncio
async def test_passkey_only_account_cannot_fall_back_to_email_on_oauth_page(client, monkeypatch):
    from app.models import Passkey

    sent = []

    async def capture(*args, **_kwargs):
        sent.append(args)

    monkeypatch.setattr("app.features.identity.oauth.controllers.oauth.send_login_code", capture)
    session, generator, user = await _rick()
    try:
        session.add(Passkey(id="pkey_test", user_id=user.id, credential_id="cred-test", public_key="AA", sign_count=0))
        await session.commit()
    finally:
        await generator.aclose()
    saved = _no_auth(client)
    try:
        _query, signed = await _oauth_login(client, "Passkey Agent")
        # The page hands the passkey step to the console. No code is emailed and no code form is shown.
        assert signed.status_code == 200
        assert "/connect-passkey#" in signed.text
        assert 'action="/oauth/login/code"' not in signed.text
        assert "set-cookie" not in signed.headers
        assert sent == []
    finally:
        if saved:
            client.headers["Authorization"] = saved


# L-10, L-11, L-12, I-04: authorization server.


@pytest.mark.asyncio
async def test_malformed_authorize_request_does_not_redirect(client):
    registered = await client.post(
        "/oauth/register", json={"client_name": "Redirector", "redirect_uris": ["https://evil.example/"]}
    )
    response = await client.get(
        "/oauth/authorize",
        params={
            "client_id": registered.json()["client_id"],
            "redirect_uri": "https://evil.example/",
            "response_type": "token",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "location" not in response.headers


@pytest.mark.asyncio
async def test_authorize_rejects_a_challenge_that_is_not_s256_shaped(client):
    registered = await client.post(
        "/oauth/register", json={"client_name": "Short", "redirect_uris": ["http://127.0.0.1:8765/callback"]}
    )
    response = await client.get(
        "/oauth/authorize",
        params={
            "client_id": registered.json()["client_id"],
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "response_type": "code",
            "code_challenge": "x" * 200,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_token_responses_are_not_cacheable_and_refresh_reuse_ends_the_family(client):
    _access, refresh, client_id = await oauth_tokens(client, "Rotating Agent")
    saved = client.headers.pop("Authorization", None)
    try:
        first = await client.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id}
        )
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        rotated = first.json()["refresh_token"]

        replayed = await client.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id}
        )
        assert replayed.status_code == 400
        assert replayed.headers["cache-control"] == "no-store"
        after = await client.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": rotated, "client_id": client_id}
        )
        assert after.status_code == 400
    finally:
        if saved:
            client.headers["Authorization"] = saved


# H-02: the routed path, not the Host header, decides exemptions.


def test_forged_host_cannot_make_a_path_look_exempt(monkeypatch):
    from starlette.requests import Request

    from app.core.origin_auth import origin_allowed, request_path

    monkeypatch.setenv("ORIGIN_AUTH_SECRET", "s3cret")
    monkeypatch.setenv("K_SERVICE", "saturn-backend")
    scope = {
        "type": "http",
        "scheme": "https",
        "path": "/admin/users",
        "query_string": b"",
        "headers": [(b"host", b"api.saturn-core.com/healthz?")],
        "method": "GET",
    }
    request = Request(scope)
    assert request_path(request) == "/admin/users"
    assert origin_allowed(request_path(request), request.headers) is False


# M-11: reauthentication is rate limited.


@pytest.mark.asyncio
async def test_password_reauth_is_rate_limited(client):
    codes = [
        (await client.post("/auth/reauth", json={"password": "wrong-password"})).status_code for _ in range(9)
    ]
    assert codes[:8] == [401] * 8
    assert codes[8] == 429


# M-22: the seed admin is not re-promoted.


@pytest.mark.asyncio
async def test_seed_admin_demotion_sticks(client):
    from app.features.identity.auth.services.auth_service import auth_service

    session, generator, user = await _rick()
    try:
        user.is_platform_admin = False
        await session.flush()
        await auth_service.ensure_seed_user(session)
        assert user.is_platform_admin is False
    finally:
        await session.rollback()
        await generator.aclose()


# L-09: a cookie-only write must come from our own origin.


@pytest.mark.asyncio
async def test_cookie_writes_need_a_trusted_origin(client):
    login = await client.post("/auth/login", json={"email": "rick@saturn.ai", "password": "saturn"})
    token = login.json()["token"]
    saved = _no_auth(client)
    try:
        client.cookies.set("saturn_session", token)
        blocked = await client.patch("/auth/notices", json={"approval": True, "execute": False, "audit": False})
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "Cross-site request blocked"
        allowed = await client.patch(
            "/auth/notices",
            json={"approval": True, "execute": False, "audit": False},
            headers={"Origin": "http://localhost:3000"},
        )
        assert allowed.status_code == 200, allowed.text
        read = await client.get("/auth/notices")
        assert read.status_code == 200
    finally:
        client.cookies.clear()
        if saved:
            client.headers["Authorization"] = saved


# L-03: changing to a taken address looks the same and sends it nothing.


@pytest.mark.asyncio
async def test_email_change_does_not_reveal_taken_addresses(client, monkeypatch):
    from app.models import User

    sent: list[str] = []

    async def capture(email, *_args, **_kwargs):
        sent.append(email)

    monkeypatch.setattr("app.features.identity.auth.controllers.auth.send_email_change_code", capture)
    session, generator = await _db()
    try:
        rick = await session.get(User, get_settings().default_actor_id)
        session.add(
            User(
                id="user_taken",
                organization_id=rick.organization_id,
                email="taken@example.com",
                name="Taken",
                password_hash="x",
            )
        )
        await session.commit()
    finally:
        await generator.aclose()
    response = await client.patch("/auth/me", json={"name": "Rick", "email": "taken@example.com"})
    assert response.status_code == 200, response.text
    assert response.json()["email_change_pending"] is True
    assert sent == ["rick@saturn.ai"]


# L-02: password reset mail goes out after the response, whatever happens.


@pytest.mark.asyncio
async def test_reset_mail_failure_is_not_visible(client, monkeypatch):
    from fastapi import HTTPException

    async def broken(*_args, **_kwargs):
        raise HTTPException(502, "Could not send email")

    monkeypatch.setattr("app.features.identity.auth.controllers.auth.send_reset_code", broken)
    known = await client.post("/auth/forgot-password", json={"email": "rick@saturn.ai"})
    unknown = await client.post("/auth/forgot-password", json={"email": "nobody-else@saturn.ai"})
    assert known.status_code == unknown.status_code == 204


# L-14, L-15, L-16: MCP OAuth client.


def test_mcp_oauth_urls_get_endpoint_checks(monkeypatch):
    from app.features.mcp.oauth.services.mcp_oauth import url_allowed

    assert url_allowed("http://localhost:8000/token") is False
    assert url_allowed("https://10.0.0.4/token") is False
    assert url_allowed("https://auth.example.com:8443/token") is False
    assert url_allowed("https://metadata.google.internal/") is False
    monkeypatch.setattr(
        "app.features.mcp.servers.services.mcp_endpoint.addresses_for", lambda _host, _port: ["10.1.2.3"]
    )
    assert url_allowed("https://internal.example.com/token") is False
    monkeypatch.setattr(
        "app.features.mcp.servers.services.mcp_endpoint.addresses_for", lambda _host, _port: ["1.1.1.1"]
    )
    assert url_allowed("https://auth.example.com/token") is True


def test_preset_clients_only_serve_their_own_hosts(monkeypatch):
    from app.features.mcp.oauth.services.mcp_oauth import GITHUB_ISSUER, granted_scope, preset_client

    monkeypatch.setenv("SATURN_GITHUB_OAUTH_CLIENT_ID", "gh-client")
    assert preset_client(GITHUB_ISSUER, "https://api.githubcopilot.com/mcp/") is not None
    assert preset_client(GITHUB_ISSUER, "https://mcp.notion.com/mcp") is None
    assert granted_scope(GITHUB_ISSUER, ["repo", "admin:org", "delete_repo"]) == "repo"


def test_mcp_callback_script_cannot_be_closed_by_error_text():
    from app.features.mcp.oauth.services.mcp_oauth import _page

    html = _page(False, "nonce-1", "</script><script>alert(1)</script>").body.decode()
    script = html[html.index("<script") :]
    assert "</script><script>" not in script


# I-10 (main.py part): the open probe says only up or down.


@pytest.mark.asyncio
async def test_healthz_reveals_nothing_else(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_browsers_get_the_session_only_as_an_httponly_cookie(client):
    # Script on the console page must not be able to read the session token.
    invited = await client.post("/org/invites", json={"email": "cookie@saturn.ai", "name": "Cookie", "role": "viewer"})
    assert invited.status_code == 201, invited.text
    accepted = await client.post(
        "/auth/accept-invite",
        headers={"Origin": "http://localhost:3000"},
        json={"token": invited.json()["token"], "password": "password1"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["token"] is None
    assert accepted.json()["user"]["email"] == "cookie@saturn.ai"
    assert "HttpOnly" in accepted.headers.get("set-cookie", "")

    browser = await client.post(
        "/auth/login", headers={"Origin": "http://localhost:3000"}, json={"email": "cookie@saturn.ai", "password": "password1"}
    )
    assert browser.status_code == 200, browser.text
    assert browser.json()["token"] is None
    api_client = await client.post("/auth/login", json={"email": "cookie@saturn.ai", "password": "password1"})
    assert api_client.status_code == 200, api_client.text
    assert api_client.json()["token"].startswith("sat_")

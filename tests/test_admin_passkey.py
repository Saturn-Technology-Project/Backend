import json
import re
from urllib.parse import parse_qs, urlparse

from app.core.config import get_settings
from app.features.identity.oauth.services.oauth_service import pkce_s256
from tests.passkey_access import ORIGIN, enrol

LOGIN = {"email": "rick@saturn.ai", "password": "saturn"}


async def _admin_with_passkey(*, email_mfa: bool):
    """The seeded admin, with a passkey, no authenticator app, and emailed codes on or off."""
    from app.db.database import get_session
    from app.main import app
    from app.models import User

    generator = app.dependency_overrides[get_session]()
    session = await anext(generator)
    try:
        user = await session.get(User, get_settings().default_actor_id)
        user.totp_enabled = False
        user.totp_secret = ""
        user.email_mfa = email_mfa
        passkey = await enrol(session, user.id)
        await session.commit()
    finally:
        await generator.aclose()
    return passkey


async def test_admin_with_a_passkey_cannot_finish_sign_in_with_an_emailed_code(client, monkeypatch):
    sent = {}

    async def capture(_email, code, _locale=None):
        sent["code"] = code

    monkeypatch.setattr("app.features.identity.auth.controllers.auth.send_login_code", capture)
    passkey = await _admin_with_passkey(email_mfa=True)
    client.headers.pop("Authorization", None)

    started = await client.post("/auth/login", json=LOGIN)
    assert started.status_code == 200, started.text
    challenge = started.json()["challenge"]
    assert set(started.json()["methods"]) == {"email", "passkey"}

    emailed = await client.post("/auth/login/email", json={"challenge": challenge, "code": sent["code"]})
    assert emailed.status_code == 200, emailed.text
    body = emailed.json()
    assert body["mfa_required"] is True
    assert body["token"] is None and body["user"] is None
    assert body["methods"] == ["passkey"]
    assert body["challenge"] == challenge
    assert "set-cookie" not in emailed.headers

    # The emailed code is spent: it cannot be tried again in place of the passkey.
    again = await client.post("/auth/login/email", json={"challenge": challenge, "code": sent["code"]})
    assert again.status_code == 400

    options = await client.post("/auth/login/passkey/options", json={"challenge": challenge}, headers={"Origin": ORIGIN})
    assert options.status_code == 200, options.text
    signed = await client.post(
        "/auth/login/passkey",
        json={"challenge": challenge, "credential": passkey.answer(options.json())},
        headers={"Origin": ORIGIN},
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["user"]["email"] == LOGIN["email"]


async def _authorize_query(client, name: str) -> dict[str, str]:
    redirect_uri = "http://127.0.0.1:8765/callback"
    registered = await client.post("/oauth/register", json={"client_name": name, "redirect_uris": [redirect_uri]})
    assert registered.status_code == 201, registered.text
    return {
        "response_type": "code",
        "client_id": registered.json()["client_id"],
        "redirect_uri": redirect_uri,
        "code_challenge": pkce_s256("c" * 43),
        "code_challenge_method": "S256",
        "state": name,
    }


def _handoff(page: str) -> dict[str, str]:
    """The fields the OAuth page hands to the console's passkey page, read from its link."""
    link = re.search(r'href="([^"]+/connect-passkey#[^"]+)"', page)
    assert link is not None, page
    fragment = urlparse(link.group(1).replace("&amp;", "&")).fragment
    return {key: values[0] for key, values in parse_qs(fragment).items()}


async def test_agent_sign_in_page_accepts_a_passkey_through_the_console(client):
    passkey = await _admin_with_passkey(email_mfa=False)
    client.headers.pop("Authorization", None)
    query = await _authorize_query(client, "passkey-agent")

    page = await client.post("/oauth/login", data={**query, **LOGIN}, follow_redirects=False)
    assert page.status_code == 200, page.text
    handoff = _handoff(page.text)
    assert {key: handoff[key] for key in query} == query

    options = await client.post(
        "/auth/login/passkey/options", json={"challenge": handoff["challenge"]}, headers={"Origin": ORIGIN}
    )
    assert options.status_code == 200, options.text
    credential = passkey.answer(options.json())

    # Without the console's origin the answer is refused.
    refused = await client.post(
        "/oauth/login/passkey",
        data={**handoff, "credential": json.dumps(credential)},
        follow_redirects=False,
    )
    assert refused.status_code == 400

    options = await client.post(
        "/auth/login/passkey/options", json={"challenge": handoff["challenge"]}, headers={"Origin": ORIGIN}
    )
    signed = await client.post(
        "/oauth/login/passkey",
        data={**handoff, "credential": json.dumps(passkey.answer(options.json()))},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert signed.status_code == 303, signed.text
    assert signed.headers["location"].startswith("/oauth/authorize?")
    consent = await client.get(signed.headers["location"])
    assert consent.status_code == 200
    assert 'name="csrf"' in consent.text


async def test_agent_sign_in_page_asks_an_admin_for_the_passkey_after_the_emailed_code(client, monkeypatch):
    sent = {}

    async def capture(_email, code, _locale=None):
        sent["code"] = code

    monkeypatch.setattr("app.features.identity.oauth.controllers.oauth.send_login_code", capture)
    await _admin_with_passkey(email_mfa=True)
    client.headers.pop("Authorization", None)
    query = await _authorize_query(client, "email-then-passkey")

    page = await client.post("/oauth/login", data={**query, **LOGIN}, follow_redirects=False)
    assert page.status_code == 200, page.text
    assert 'name="method" value="email"' in page.text
    challenge = _handoff(page.text)["challenge"]

    after = await client.post(
        "/oauth/login/code",
        data={**query, "challenge": challenge, "method": "email", "code": sent["code"]},
        follow_redirects=False,
    )
    # No consent session yet: the page asks for the passkey instead.
    assert after.status_code == 200, after.text
    assert "set-cookie" not in after.headers
    assert _handoff(after.text)["challenge"] == challenge

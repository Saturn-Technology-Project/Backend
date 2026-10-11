import re
from urllib.parse import parse_qs, urlparse

from app.features.identity.oauth.services.oauth_service import pkce_s256


async def oauth_tokens(
    client,
    client_name: str,
    *,
    email: str = "rick@saturn.ai",
    password: str = "saturn",
) -> tuple[str, str, str]:
    saved = client.headers.get("Authorization")
    client.headers.pop("Authorization", None)
    verifier = "c" * 43
    challenge = pkce_s256(verifier)
    registered = await client.post(
        "/oauth/register",
        json={"client_name": client_name, "redirect_uris": ["http://127.0.0.1:8765/callback"]},
    )
    assert registered.status_code == 201, registered.text
    query = {
        "response_type": "code",
        "client_id": registered.json()["client_id"],
        "redirect_uri": "http://127.0.0.1:8765/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": client_name,
    }
    await client.get("/oauth/authorize", params=query)
    signed = await client.post(
        "/oauth/login",
        data={**query, "email": email, "password": password},
        follow_redirects=False,
    )
    assert signed.status_code == 303, signed.text
    consent = await client.get(signed.headers["location"])
    csrf = re.search(r'name="csrf" value="([^"]+)"', consent.text)
    assert csrf is not None
    allowed = await client.post(
        "/oauth/consent",
        data={**query, "csrf": csrf.group(1), "decision": "allow"},
        follow_redirects=False,
    )
    assert allowed.status_code == 303, allowed.text
    code = parse_qs(urlparse(allowed.headers["location"]).query)["code"][0]
    token = await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": query["client_id"],
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    if saved:
        client.headers["Authorization"] = saved
    body = token.json()
    return body["access_token"], body["refresh_token"], query["client_id"]


async def oauth_access(
    client,
    client_name: str,
    *,
    email: str = "rick@saturn.ai",
    password: str = "saturn",
) -> str:
    access, _refresh, _client_id = await oauth_tokens(client, client_name, email=email, password=password)
    return access

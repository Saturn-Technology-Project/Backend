from __future__ import annotations

import secrets
from html import escape
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.features.identity.auth.controllers.deps import SessionDep
from app.features.identity.auth.services.auth_service import auth_service
from app.features.identity.oauth.services.oauth_service import SCOPE, oauth_service, redirect_allowed
from app.features.organization.org.services.plans import profile_identity

router = APIRouter(tags=["oauth"])
COOKIE = "saturn_oauth"


def origin_of(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def metadata(origin: str) -> dict:
    return {
        "issuer": origin,
        "authorization_endpoint": f"{origin}/oauth/authorize",
        "token_endpoint": f"{origin}/oauth/token",
        "registration_endpoint": f"{origin}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE],
    }


def protected_resource(origin: str) -> dict:
    return {
        "resource": f"{origin}/mcp",
        "authorization_servers": [origin],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [SCOPE],
    }


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ margin: 0; background: #f7f7f5; color: #111110; font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; }}
    main {{ max-width: 420px; margin: 48px auto; padding: 0 20px; }}
    h1 {{ font-size: 22px; font-weight: 560; letter-spacing: -0.02em; }}
    p {{ color: #5c5c57; }}
    label {{ display: block; margin-top: 14px; font-size: 12px; color: #5c5c57; }}
    input {{ width: 100%; box-sizing: border-box; margin-top: 6px; height: 36px; border: 1px solid #e6e6e1; border-radius: 8px; background: #fff; padding: 0 10px; }}
    .row {{ display: flex; gap: 8px; margin-top: 18px; }}
    button {{ height: 36px; border: 0; border-radius: 8px; padding: 0 14px; background: #171716; color: #f7f7f5; }}
    button.secondary {{ background: #efefe9; color: #111110; }}
    code {{ display: block; margin-top: 8px; padding: 8px 10px; border: 1px solid #e6e6e1; border-radius: 8px; background: #fff; word-break: break-all; }}
  </style>
</head>
<body><main>{body}</main></body>
</html>"""
    return HTMLResponse(html, status_code=status_code)


def _hidden(fields: dict[str, str]) -> str:
    return "".join(
        f'<input type="hidden" name="{escape(key)}" value="{escape(value)}">'
        for key, value in fields.items()
        if value
    )


async def _posted(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", errors="replace")
    return dict(parse_qsl(raw, keep_blank_values=True))


def _fields(request: Request) -> dict[str, str]:
    source = request.query_params
    return {
        "client_id": source.get("client_id", ""),
        "redirect_uri": source.get("redirect_uri", ""),
        "state": source.get("state", ""),
        "code_challenge": source.get("code_challenge", ""),
        "code_challenge_method": source.get("code_challenge_method", ""),
        "response_type": source.get("response_type", ""),
    }


def _cookie_parts(request: Request) -> tuple[str, str]:
    raw = request.cookies.get(COOKIE, "")
    token, _, csrf = raw.partition(".")
    return token, csrf


def _set_cookie(response: Response, request: Request, token: str, csrf: str) -> None:
    response.set_cookie(
        COOKIE,
        f"{token}.{csrf}",
        httponly=True,
        samesite="lax",
        max_age=3600,
        path="/oauth",
        secure=request.url.scheme == "https",
    )


def _redirect(uri: str, **params: str) -> RedirectResponse:
    parsed = urlparse(uri)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: value for key, value in params.items() if value})
    target = urlunparse(parsed._replace(query=urlencode(query)))
    return RedirectResponse(target, status_code=303)


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata(request: Request) -> dict:
    return protected_resource(origin_of(request))


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(request: Request) -> dict:
    return metadata(origin_of(request))


@router.post("/oauth/register", status_code=201)
async def register_client(request: Request, session: SessionDep) -> dict:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    name = str(body.get("client_name") or "Agent").strip() or "Agent"
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > 8:
        raise HTTPException(400, "redirect_uris are required")
    cleaned = []
    for item in uris:
        uri = str(item or "").strip()
        if not redirect_allowed(uri):
            raise HTTPException(400, "redirect_uri must be https, localhost, or an app callback")
        cleaned.append(uri)
    client = await oauth_service.register(session, name=name, redirect_uris=cleaned)
    origin = origin_of(request)
    return {
        "client_id": client.id,
        "client_name": client.name,
        "redirect_uris": cleaned,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
        "client_id_issued_at": int(client.created_at.timestamp()),
        "authorization_endpoint": f"{origin}/oauth/authorize",
        "token_endpoint": f"{origin}/oauth/token",
    }


@router.get("/oauth/authorize")
async def authorize(request: Request, session: SessionDep):
    fields = _fields(request)
    client = await oauth_service.client_for(session, fields["client_id"])
    if client is None or not oauth_service.matching_redirect(client, fields["redirect_uri"]):
        return _page("Saturn", "<h1>無法連線</h1><p>This agent is not registered, or the redirect URL does not match.</p>", 400)
    if fields["response_type"] != "code" or fields["code_challenge_method"] != "S256" or not fields["code_challenge"]:
        return _redirect(fields["redirect_uri"], error="invalid_request", state=fields["state"])
    token, _csrf = _cookie_parts(request)
    user = await auth_service.user_from_token(session, token) if token else None
    if user is None or user.organization is None:
        return _page(
            "Connect Agents",
            f"""<h1>連接代理人</h1>
            <p>{escape(client.name)} wants to connect to Saturn MCP. Sign in to bind it to your profile.</p>
            <form method="post" action="/oauth/login">
              {_hidden(fields)}
              <label>電子郵件 Email<input name="email" type="email" autocomplete="username" required></label>
              <label>密碼 Password<input name="password" type="password" autocomplete="current-password" required></label>
              <div class="row"><button type="submit">登入 Sign in</button></div>
            </form>""",
        )
    identity = profile_identity(user.id, user.organization_id, user.organization.kind, user.organization.plan)
    csrf = secrets.token_urlsafe(18)
    page = _page(
        "Connect Agents",
        f"""<h1>連接代理人</h1>
        <p>{escape(client.name)} will connect to Saturn MCP as this profile.</p>
        <label>身分識別碼 Identity</label>
        <code>{escape(identity)}</code>
        <form method="post" action="/oauth/consent">
          {_hidden(fields)}
          <input type="hidden" name="csrf" value="{escape(csrf)}">
          <div class="row">
            <button type="submit" name="decision" value="allow">允許 Allow</button>
            <button class="secondary" type="submit" name="decision" value="deny">拒絕 Deny</button>
          </div>
        </form>""",
    )
    _set_cookie(page, request, token, csrf)
    return page


@router.post("/oauth/login")
async def oauth_login(request: Request, session: SessionDep):
    form = await _posted(request)
    fields = {key: form.get(key, "") for key in ("client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "response_type")}
    try:
        token, _user = await auth_service.login(session, form.get("email", ""), form.get("password", ""))
    except HTTPException:
        return _page("Connect Agents", "<h1>登入失敗</h1><p>Email or password is incorrect.</p>", 401)
    csrf = secrets.token_urlsafe(18)
    response = RedirectResponse("/oauth/authorize?" + urlencode(fields), status_code=303)
    _set_cookie(response, request, token, csrf)
    return response


@router.post("/oauth/consent")
async def oauth_consent(request: Request, session: SessionDep):
    form = await _posted(request)
    fields = {key: form.get(key, "") for key in ("client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "response_type")}
    token, csrf = _cookie_parts(request)
    posted_csrf = form.get("csrf", "")
    if not csrf or len(csrf) != len(posted_csrf) or not secrets.compare_digest(csrf, posted_csrf):
        return _page("Saturn", "<h1>無法連線</h1><p>Sign in again to connect this agent.</p>", 400)
    user = await auth_service.user_from_token(session, token) if token else None
    client = await oauth_service.client_for(session, fields["client_id"]) if user is not None else None
    if user is None or client is None or not oauth_service.matching_redirect(client, fields["redirect_uri"]):
        return _page("Saturn", "<h1>無法連線</h1><p>Sign in again to connect this agent.</p>", 400)
    if form.get("decision", "") != "allow":
        return _redirect(fields["redirect_uri"], error="access_denied", state=fields["state"])
    code = await oauth_service.issue_code(
        session,
        client=client,
        user=user,
        redirect_uri=fields["redirect_uri"],
        code_challenge=fields["code_challenge"],
    )
    return _redirect(fields["redirect_uri"], code=code, state=fields["state"])


@router.post("/oauth/token")
async def oauth_token(request: Request, session: SessionDep) -> dict:
    form = await _posted(request)
    grant = form.get("grant_type", "")
    client_id = form.get("client_id", "")
    client = await oauth_service.client_for(session, client_id)
    if client is None:
        raise HTTPException(400, "invalid_client")
    if grant == "authorization_code":
        issued = await oauth_service.exchange_code(
            session,
            code=form.get("code", ""),
            client_id=client_id,
            redirect_uri=form.get("redirect_uri", ""),
            code_verifier=form.get("code_verifier", ""),
        )
    elif grant == "refresh_token":
        issued = await oauth_service.refresh(
            session,
            refresh_token=form.get("refresh_token", ""),
            client_id=client_id,
        )
    else:
        raise HTTPException(400, "unsupported_grant_type")
    if issued is None:
        raise HTTPException(400, "invalid_grant")
    access, refresh, expires_in = issued
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "refresh_token": refresh,
        "scope": SCOPE,
    }

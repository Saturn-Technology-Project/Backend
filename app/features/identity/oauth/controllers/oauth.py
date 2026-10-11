from __future__ import annotations

import secrets
from html import escape
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.enums import ActorType
from app.features.execution.activity.services.audit_service import audit_service
from app.features.identity.auth.controllers.deps import SessionDep
from app.features.identity.auth.services.auth_service import auth_service
from app.features.identity.auth.services.mailer import is_english, normalize_locale, send_login_code
from app.features.identity.auth.services.mfa import mfa_service
from app.features.identity.oauth.services.oauth_service import ACCESS_TTL, REFRESH_TTL, SCOPE, oauth_service, redirect_allowed
from app.features.organization.org.services.plans import profile_identity
from app.models import Agent, Policy, ToolSet, ToolSetItem

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


def _page(title: str, body: str, status_code: int = 200, *, wide: bool = False, lang: str = "zh-Hant") -> HTMLResponse:
    width = "520px" if wide else "420px"
    html = f"""<!doctype html>
<html lang="{escape(lang)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ margin: 0; background: #f7f7f5; color: #111110; font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; }}
    main {{ max-width: {width}; margin: 48px auto; padding: 0 20px 48px; }}
    h1 {{ margin: 8px 0 0; font-size: 22px; font-weight: 600; letter-spacing: -0.02em; }}
    h2 {{ margin: 22px 0 0; font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: #8a8a84; }}
    p {{ color: #5c5c57; }}
    .kicker {{ margin: 0; font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: #8a8a84; }}
    .card {{ margin-top: 8px; padding: 12px 14px; border: 1px solid #e6e6e1; border-radius: 10px; background: #fff; }}
    .card strong {{ display: block; color: #111110; font-size: 14px; }}
    .card p, .note {{ margin: 4px 0 0; }}
    label {{ display: block; margin-top: 14px; font-size: 12px; color: #5c5c57; }}
    input {{ width: 100%; box-sizing: border-box; margin-top: 6px; height: 36px; border: 1px solid #e6e6e1; border-radius: 8px; background: #fff; padding: 0 10px; }}
    .legal {{ margin-top: 22px; font-size: 12px; line-height: 1.6; }}
    .legal a {{ color: #111110; }}
    .row {{ display: flex; gap: 8px; margin-top: 18px; }}
    .row button {{ flex: 1; }}
    button {{ height: 36px; border: 0; border-radius: 8px; padding: 0 14px; background: #171716; color: #f7f7f5; cursor: pointer; }}
    button.secondary {{ background: #efefe9; color: #111110; }}
    code {{ display: block; margin-top: 8px; padding: 8px 10px; border: 1px solid #e6e6e1; border-radius: 8px; background: #fff; word-break: break-all; color: #111110; }}
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


def _legal_links(locale: str) -> tuple[str, str]:
    if is_english(locale):
        return "https://saturn.technology/terms", "https://saturn.technology/privacy"
    return "https://saturn.technology/zh/terms", "https://saturn.technology/zh/privacy"


async def _consent_scope(session, user, client_id: str) -> dict:
    agents = await session.execute(select(Agent).where(Agent.organization_id == user.organization_id))
    agent = next(
        (
            item
            for item in agents.scalars()
            if (item.settings or {}).get("origin") == "saturn_mcp"
            and (item.settings or {}).get("client_id") == client_id
        ),
        None,
    )
    set_name = None
    approval_tools = False
    if agent is not None and agent.tool_set_id:
        tool_set = await session.get(ToolSet, agent.tool_set_id)
        if tool_set is not None and tool_set.organization_id == user.organization_id:
            set_name = tool_set.name
            items = await session.execute(select(ToolSetItem).where(ToolSetItem.tool_set_id == tool_set.id))
            approval_tools = any(item.approval for item in items.scalars())
    policies = await session.execute(
        select(Policy)
        .options(selectinload(Policy.agents))
        .where(Policy.organization_id == user.organization_id)
    )
    names: list[str] = []
    for policy in policies.scalars():
        if (policy.status or "active").lower() != "active":
            continue
        bound = list(policy.agents or [])
        if agent is None:
            applies = False
        elif not bound:
            applies = True
        else:
            applies = any(item.id == agent.id for item in bound)
        if applies:
            names.append(policy.name)
    return {
        "connected": agent is not None,
        "set_name": set_name,
        "policies": names,
        "approval_tools": approval_tools,
    }


def _consent_body(locale: str, client_name: str, client_id: str, identity: str, scope: dict, fields: dict[str, str], csrf: str) -> str:
    terms, privacy = _legal_links(locale)
    hours = max(1, int(ACCESS_TTL.total_seconds() // 3600))
    days = REFRESH_TTL.days
    set_name = scope["set_name"]
    policy_name = "、".join(scope["policies"]) if scope["policies"] else ""
    if is_english(locale):
        if scope["connected"]:
            set_label = set_name or "Not assigned"
            set_detail = (
                "This agent can use the tools and operations explicitly allowed in this set."
                if set_name
                else "No set is assigned. This agent cannot call tools until one is."
            )
            policy_label = policy_name or "None applied"
            approval_detail = "A call runs only when the rules allow it. Anything that needs approval waits for that decision."
            if scope["approval_tools"]:
                approval_detail += " Some tools in this set are marked as requiring approval."
            access = f"""<div class="card"><strong>Set: {escape(set_label)}</strong><p>{escape(set_detail)}</p></div>
        <div class="card"><strong>Policy: {escape(policy_label)}</strong><p>Saturn checks permission rules, denials, and approval requirements before a tool runs.</p></div>
        <div class="card"><strong>Approval required</strong><p>{escape(approval_detail)}</p></div>"""
        else:
            access = """<div class="card"><strong>Connection only</strong><p>This first authorization does not include a set or a policy. After the agent appears in the console, assign the tools it may use and the policy that applies. Until a set is assigned, this agent cannot call tools.</p></div>"""
        return f"""<p class="kicker">Connect to Saturn</p>
        <h1>Agent authorization request</h1>
        <h2>Requesting agent</h2>
        <div class="card"><strong>{escape(client_name)}</strong><p>Name supplied by the agent developer. Client {escape(client_id)}</p></div>
        <h2>Access requested</h2>
        {access}
        <h2>Before you connect</h2>
        <p class="note">Connecting does not grant unlimited access. Saturn checks each tool call against the granted set, the active policy, and the approval rules, and writes the call to the audit log. You can revoke this connection from the console.</p>
        <p class="note">Access lasts {hours} hour for the access token and {days} days for the refresh token, or until you revoke it.</p>
        <label>Connecting as</label>
        <code>{escape(identity)}</code>
        <p class="legal">By continuing, you acknowledge the <a href="{terms}" target="_blank" rel="noopener">Terms of Service</a> and <a href="{privacy}" target="_blank" rel="noopener">Privacy Policy</a>.</p>
        <form method="post" action="/oauth/consent">
          {_hidden(fields)}
          <input type="hidden" name="csrf" value="{escape(csrf)}">
          <div class="row">
            <button class="secondary" type="submit" name="decision" value="deny">Deny</button>
            <button type="submit" name="decision" value="allow">Authorize agent</button>
          </div>
        </form>"""
    if scope["connected"]:
        set_label = set_name or "尚未指定"
        set_detail = (
            "可使用此 Set 中明確允許的工具與操作。"
            if set_name
            else "尚未指定 Set。在指定之前，這個代理人不能呼叫工具。"
        )
        policy_label = policy_name or "尚未套用"
        approval_detail = "符合規則時才執行。需要核准的操作必須先經過核准。"
        if scope["approval_tools"]:
            approval_detail += "這個 Set 裡有工具被標成需要核准。"
        access = f"""<div class="card"><strong>Set：{escape(set_label)}</strong><p>{escape(set_detail)}</p></div>
    <div class="card"><strong>Policy：{escape(policy_label)}</strong><p>受 Saturn 的權限規則、拒絕條件與核准要求約束。</p></div>
    <div class="card"><strong>需要核准</strong><p>{escape(approval_detail)}</p></div>"""
    else:
        access = """<div class="card"><strong>僅建立連線</strong><p>首次授權還沒有 Set，也還沒有套用 Policy。代理人出現在控制台後，再指定可以使用的工具與要套用的政策。在指定 Set 之前，這個代理人不能呼叫工具。</p></div>"""
    return f"""<p class="kicker">連接 Saturn</p>
    <h1>代理人授權請求</h1>
    <h2>申請連線的代理人</h2>
    <div class="card"><strong>{escape(client_name)}</strong><p>由代理人開發者提供的名稱。識別碼 {escape(client_id)}</p></div>
    <h2>要求的存取</h2>
    {access}
    <h2>連線之前</h2>
    <p class="note">連線不代表授予無限制存取權。Saturn 會依照已授予的 Set、有效 Policy 與審批規則檢查每次工具呼叫，並寫入稽核紀錄。你可以在控制台撤銷這次連線。</p>
    <p class="note">存取權杖 {hours} 小時，重新整理權杖 {days} 天，或直到撤銷。</p>
    <label>以此身分連線</label>
    <code>{escape(identity)}</code>
    <p class="legal">繼續即表示你知悉 <a href="{terms}" target="_blank" rel="noopener">服務條款</a> 與 <a href="{privacy}" target="_blank" rel="noopener">隱私權政策</a>。</p>
    <form method="post" action="/oauth/consent">
      {_hidden(fields)}
      <input type="hidden" name="csrf" value="{escape(csrf)}">
      <div class="row">
        <button class="secondary" type="submit" name="decision" value="deny">拒絕</button>
        <button type="submit" name="decision" value="allow">授權代理人</button>
      </div>
    </form>"""


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
    scope = await _consent_scope(session, user, client.id)
    locale = normalize_locale(user.locale)
    page = _page(
        "Authorize agent" if is_english(locale) else "授權代理人",
        _consent_body(locale, client.name, client.id, identity, scope, fields, csrf),
        wide=True,
        lang=locale,
    )
    _set_cookie(page, request, token, csrf)
    return page


@router.post("/oauth/login")
async def oauth_login(request: Request, session: SessionDep):
    form = await _posted(request)
    fields = {key: form.get(key, "") for key in ("client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "response_type")}
    try:
        user = await auth_service.authenticate(session, form.get("email", ""), form.get("password", ""))
    except HTTPException:
        return _page("Connect Agents", "<h1>登入失敗</h1><p>Email or password is incorrect.</p>", 401)
    try:
        pending = await mfa_service.start_login(session, user, include_email=True)
    except HTTPException as exc:
        return _page("Connect Agents", f"<h1>請稍後再試</h1><p>{escape(str(exc.detail))}</p>", exc.status_code)
    if pending is not None:
        challenge, _methods, code = pending
        if code is not None:
            try:
                await send_login_code(user.email, code, user.locale)
            except HTTPException as exc:
                await mfa_service.discard_login(session, challenge)
                return _page("Connect Agents", f"<h1>無法寄信</h1><p>{escape(str(exc.detail))}</p>", exc.status_code)
        return _page(
            "Verify sign-in",
            f"""<h1>驗證登入</h1>
            <p>We emailed a sign-in code. Enter it to continue.</p>
            <form method="post" action="/oauth/login/code">
              {_hidden(fields)}
              <input type="hidden" name="challenge" value="{escape(challenge)}">
              <label>驗證碼 Code<input name="code" inputmode="numeric" autocomplete="one-time-code" required></label>
              <div class="row"><button type="submit">繼續 Continue</button></div>
            </form>""",
        )
    token, _user = await auth_service.issue_session(session, user)
    csrf = secrets.token_urlsafe(18)
    response = RedirectResponse("/oauth/authorize?" + urlencode(fields), status_code=303)
    _set_cookie(response, request, token, csrf)
    return response


@router.post("/oauth/login/code")
async def oauth_login_code(request: Request, session: SessionDep):
    form = await _posted(request)
    fields = {key: form.get(key, "") for key in ("client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "response_type")}
    try:
        user = await mfa_service.finish_email(session, form.get("challenge", ""), form.get("code", ""))
    except HTTPException:
        return _page("Connect Agents", "<h1>驗證失敗</h1><p>That code is incorrect or expired.</p>", 400)
    token, _user = await auth_service.issue_session(session, user)
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
    scope = await _consent_scope(session, user, client.id)
    identity = profile_identity(user.id, user.organization_id, user.organization.kind, user.organization.plan)
    await audit_service.log(
        session,
        event="agent_authorized",
        actor_type=ActorType.USER,
        actor_id=user.id,
        organization_id=user.organization_id,
        result="allow",
        payload={
            "client_id": client.id,
            "client_name": client.name,
            "tool_set": scope["set_name"],
            "policies": scope["policies"],
            "profile_id": identity,
        },
    )
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

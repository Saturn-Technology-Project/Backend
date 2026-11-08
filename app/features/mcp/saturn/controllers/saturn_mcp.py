from typing import Any

import json
import secrets

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.public_origin import public_origin
from app.features.identity.auth.controllers.deps import SessionDep
from app.models import Organization, User
from app.features.identity.oauth.services.oauth_service import oauth_service
from app.features.organization.org.services.plans import allows_saturn_cloud, profile_identity
from app.features.mcp.saturn.services.mcp_sessions import mcp_sessions
from app.features.mcp.saturn.services.saturn_mcp import rpc_error, saturn_mcp

router = APIRouter(tags=["saturn-mcp"])


def _unauthorized(origin: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Not authenticated",
        headers={"WWW-Authenticate": f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource"'},
    )


def _http_origin(request: Request) -> str:
    # PUBLIC_WS_URL, not the Host header: a client must not choose where metadata points.
    origin = public_origin(request)
    if origin.startswith("wss://"):
        return "https://" + origin.removeprefix("wss://")
    if origin.startswith("ws://"):
        return "http://" + origin.removeprefix("ws://")
    return origin


async def _caller(request: Request, session: SessionDep) -> tuple[User, str, str]:
    origin = _http_origin(request)
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        raise _unauthorized(origin)
    oauth_user = await oauth_service.user_from_access_token(session, token)
    if oauth_user is None:
        raise _unauthorized(origin)
    user, client_name, client_id = oauth_user
    if not client_id:
        raise _unauthorized(origin)
    org = user.organization or await session.get(Organization, user.organization_id)
    if org is None or not allows_saturn_cloud(org.plan):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Saturn MCP is unavailable")
    return user, client_name, client_id


MAX_MCP_BODY_BYTES = 256 * 1024
MCP_CALLS_PER_MINUTE = 600


async def _read_capped(request: Request) -> bytes | None:
    """The request body, or None once it passes MAX_MCP_BODY_BYTES. Never buffers more than that."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_MCP_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_MCP_BODY_BYTES:
            return None
    return bytes(body)


@router.post("/mcp")
async def saturn_mcp_endpoint(request: Request, session: SessionDep) -> JSONResponse:
    try:
        user, client_name, client_id = await _caller(request, session)
    except HTTPException:
        raise
    org = user.organization or await session.get(Organization, user.organization_id)
    from app.core.rate_limit import allow

    if not allow(f"mcp:{user.id}:{client_id}", limit=MCP_CALLS_PER_MINUTE, window=60):
        return JSONResponse(
            rpc_error(None, -32029, "Too many requests"),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "60"},
        )
    raw = await _read_capped(request)
    if raw is None:
        return JSONResponse(
            rpc_error(None, -32600, "Request is too large"),
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )
    try:
        message = json.loads(raw) if raw else {}
    except Exception:
        message = {}
    if not isinstance(message, dict):
        message = {}
    expected = profile_identity(user.id, org.id, org.kind, org.plan) if org is not None else ""
    presented = (request.headers.get("x-saturn-profile") or "").strip()
    if presented and expected and presented != expected:
        return JSONResponse(rpc_error(message.get("id", 1), -32000, "Profile does not match"))
    method = str(message.get("method") or "")
    incoming = (request.headers.get("mcp-session-id") or "").strip()
    if method == "initialize":
        # Session ids are issued here only. A client cannot pick one, or reuse another caller's.
        session_id = secrets.token_urlsafe(18)
    elif incoming:
        if await mcp_sessions.get(incoming, user.id, client_id) is None:
            return JSONResponse(
                rpc_error(message.get("id", 1), -32000, "Session not found. Send initialize to start a new session."),
                status_code=status.HTTP_404_NOT_FOUND,
            )
        session_id = incoming
    else:
        session_id = ""
    body = await saturn_mcp.handle(
        session,
        user,
        agent_id=request.headers.get("x-saturn-agent-id", ""),
        profile_id=presented or expected,
        client_name=client_name,
        client_id=client_id,
        mcp_session_id=session_id,
        message=message,
    )
    issued = bool(session_id) and (method != "initialize" or "result" in body)
    headers = {"Mcp-Session-Id": session_id} if issued else {}
    return JSONResponse(body, headers=headers)


@router.get("/mcp")
async def saturn_mcp_listen(request: Request, session: SessionDep) -> StreamingResponse:
    user, _client_name, client_id = await _caller(request, session)
    session_id = (request.headers.get("mcp-session-id") or "").strip()
    record = await mcp_sessions.get(session_id, user.id, client_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP session not found")

    async def events():
        try:
            yield ": connected\n\n"
            while True:
                if record.queue.empty():
                    note = await record.queue.get()
                else:
                    note = record.queue.get_nowait()
                if note is None:
                    return
                yield f"event: message\ndata: {json.dumps(note, separators=(',', ':'))}\n\n"
        finally:
            await mcp_sessions.close(session_id)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Mcp-Session-Id": session_id},
    )

from typing import Any

import json
import secrets

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

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


async def _caller(request: Request, session: SessionDep) -> tuple[User, str, str]:
    origin = str(request.base_url).rstrip("/")
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


@router.post("/mcp")
async def saturn_mcp_endpoint(request: Request, session: SessionDep) -> JSONResponse:
    try:
        user, client_name, client_id = await _caller(request, session)
    except HTTPException:
        raise
    org = user.organization or await session.get(Organization, user.organization_id)
    try:
        message = await request.json()
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
    session_id = incoming or (secrets.token_urlsafe(18) if method == "initialize" else "")
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
    headers = {"Mcp-Session-Id": session_id} if session_id else {}
    return JSONResponse(body, headers=headers)


@router.get("/mcp")
async def saturn_mcp_listen(request: Request, session: SessionDep) -> StreamingResponse:
    user, _client_name, client_id = await _caller(request, session)
    session_id = (request.headers.get("mcp-session-id") or "").strip()
    record = mcp_sessions.get(session_id, user.id, client_id)
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
            mcp_sessions.close(session_id)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Mcp-Session-Id": session_id},
    )

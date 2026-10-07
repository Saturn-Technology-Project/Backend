from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import SessionDep
from app.core.config import public_http_origin
from app.models import Organization
from app.services.auth_service import auth_service
from app.services.oauth_service import oauth_service
from app.services.plans import allows_saturn_cloud, profile_identity
from app.services.saturn_mcp import rpc_error, saturn_mcp

router = APIRouter(tags=["saturn-mcp"])


def _unauthorized(origin: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Not authenticated",
        headers={"WWW-Authenticate": f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource"'},
    )


@router.post("/mcp")
async def saturn_mcp_endpoint(request: Request, session: SessionDep) -> dict[str, Any]:
    origin = public_http_origin(str(request.base_url).rstrip("/"))
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        raise _unauthorized(origin)
    user = await auth_service.user_from_token(session, token)
    client_name = ""
    if user is None:
        oauth_user = await oauth_service.user_from_access_token(session, token)
        if oauth_user is not None:
            user, client_name = oauth_user
    if user is None:
        raise _unauthorized(origin)
    org = user.organization or await session.get(Organization, user.organization_id)
    if org is None or not allows_saturn_cloud(org.plan):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Saturn MCP requires Pro")
    try:
        message = await request.json()
    except Exception:
        message = {}
    if not isinstance(message, dict):
        message = {}
    expected = profile_identity(user.id, org.id, org.kind, org.plan)
    presented = (request.headers.get("x-saturn-profile") or "").strip()
    if presented and presented != expected:
        return rpc_error(message.get("id", 1), -32000, "Profile does not match")
    return await saturn_mcp.handle(
        session,
        user,
        agent_id=request.headers.get("x-saturn-agent-id", ""),
        run_id=request.headers.get("x-saturn-run-id", ""),
        action_id=request.headers.get("x-saturn-action-id", ""),
        profile_id=presented or (expected if client_name else ""),
        client_name=client_name,
        message=message,
    )

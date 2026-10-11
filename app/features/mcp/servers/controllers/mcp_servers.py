from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.features.identity.auth.controllers.deps import AgentsRead, CurrentUser, SaturnCloud, SessionDep, TeamWrite
from app.models import McpServer
from app.schemas import McpConnect, McpOAuthBegin, McpOAuthStart, McpServerRead, McpToolRead
from app.features.organization.org.services.mapping_service import mapping_service
from app.features.mcp.servers.services.mcp_connect import mcp_connect_service
from app.features.mcp.oauth.services.mcp_oauth import callback_origin, mcp_oauth
from app.features.organization.org.services.removal_service import removal_service
from app.features.mcp.servers.services.mcp_gateway import mcp_gateway

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])


def serialize_mcp_server(server: McpServer) -> McpServerRead:
    active = [tool for tool in (server.tools or []) if tool.status == "active"]
    tools = sorted(tool.name for tool in active)
    link = mcp_gateway.link_for(server.id)
    return McpServerRead(
        id=server.id,
        name=server.name,
        endpoint=server.endpoint,
        status=server.status,
        connected=bool(link and link.connected),
        has_authorization=bool(server.credential_id),
        gateway="mcp-gateway",
        organization_id=server.organization_id,
        tools=tools,
        catalog=[
            McpToolRead(id=tool.id, name=tool.name, description=tool.description or "")
            for tool in sorted(active, key=lambda item: item.name)
        ],
    )


@router.get("", response_model=list[McpServerRead])
async def list_mcp_servers(session: SessionDep, user: CurrentUser, _access: AgentsRead):
    await mapping_service.bind_workspace(session, user)
    servers = await mapping_service.list_mcp_servers(session, user)
    return [serialize_mcp_server(server) for server in servers]


@router.post("/oauth/start", response_model=McpOAuthStart)
async def start_mcp_oauth(
    body: McpOAuthBegin,
    request: Request,
    session: SessionDep,
    user: CurrentUser,
    _access: TeamWrite,
    _cloud: SaturnCloud,
):
    result = await mcp_oauth.begin(
        session,
        user,
        name=body.name,
        endpoint=body.endpoint,
        origin=callback_origin(str(request.base_url).rstrip("/")),
        nonce=body.nonce,
    )
    if result.get("authorization_url"):
        return McpOAuthStart(authorization_url=result["authorization_url"])
    return McpOAuthStart(server=serialize_mcp_server(result["server"]))


@router.get("/oauth/callback", response_class=HTMLResponse)
async def finish_mcp_oauth(request: Request, session: SessionDep) -> HTMLResponse:
    return await mcp_oauth.finish(
        session,
        code=request.query_params.get("code", ""),
        state=request.query_params.get("state", ""),
        error=request.query_params.get("error", ""),
    )


@router.post("", response_model=McpServerRead, status_code=201)
async def connect_mcp_server(body: McpConnect, session: SessionDep, user: CurrentUser, _access: TeamWrite, _cloud: SaturnCloud):
    server = await mcp_connect_service.connect(
        session,
        user,
        name=body.name,
        endpoint=body.endpoint,
        authorization=body.authorization,
    )
    return serialize_mcp_server(server)


@router.post("/{server_id}/refresh", response_model=McpServerRead)
async def refresh_mcp_server(server_id: str, session: SessionDep, user: CurrentUser, _access: TeamWrite, _cloud: SaturnCloud):
    server = await mcp_connect_service.refresh(session, user, server_id)
    return serialize_mcp_server(server)


@router.delete("/{server_id}", status_code=204)
async def delete_mcp_server(server_id: str, session: SessionDep, user: CurrentUser, _access: TeamWrite, _cloud: SaturnCloud):
    await removal_service.delete_mcp_server(session, user, server_id)

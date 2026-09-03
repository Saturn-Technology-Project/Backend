from fastapi import APIRouter

from app.api.deps import AgentsRead, CurrentUser, SessionDep
from app.models import McpServer
from app.schemas import McpServerRead
from app.services.mapping_service import mapping_service
from app.services.mcp_gateway import mcp_gateway

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])


def serialize_mcp_server(server: McpServer) -> McpServerRead:
    tools = sorted(tool.name for tool in (server.tools or []))
    link = mcp_gateway.link_for(server.id)
    return McpServerRead(
        id=server.id,
        name=server.name,
        endpoint=server.endpoint,
        status=server.status,
        connected=bool(link and link.connected),
        gateway="mcp-gateway",
        organization_id=server.organization_id,
        tools=tools,
    )


@router.get("", response_model=list[McpServerRead])
async def list_mcp_servers(session: SessionDep, user: CurrentUser, _access: AgentsRead):
    await mapping_service.bind_workspace(session, user)
    servers = await mapping_service.list_mcp_servers(session, user)
    return [serialize_mcp_server(server) for server in servers]

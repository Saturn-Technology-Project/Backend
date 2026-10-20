from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.features.identity.auth.controllers.deps import AgentsRead, CurrentUser, SessionDep
from app.models import Tool
from app.schemas import ToolRead
from app.features.organization.org.services.mapping_service import mapping_service
from app.shared.pagination import Page

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=list[ToolRead])
async def list_tools(session: SessionDep, user: CurrentUser, _access: AgentsRead, page: Page):
    return await mapping_service.list_org_tools(
        session, user.organization_id, limit=page.limit, offset=page.offset
    )


@router.get("/{tool_id}", response_model=ToolRead)
async def get_tool(tool_id: str, session: SessionDep, user: CurrentUser, _access: AgentsRead):
    tool = await session.get(Tool, tool_id)
    if tool is not None and tool.organization_id != user.organization_id:
        tool = None
    if tool is None:
        # Names are unique per organization (uq_tools_org_name), never across tenants.
        result = await session.execute(
            select(Tool)
            .where(Tool.organization_id == user.organization_id, Tool.name == tool_id)
            .limit(1)
        )
        tool = result.scalar_one_or_none()
    if tool is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tool not found")
    return tool

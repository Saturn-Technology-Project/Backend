from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.features.identity.auth.controllers.deps import AgentsRead, CurrentUser, SessionDep
from app.models import Tool
from app.schemas import ToolRead
from app.features.organization.org.services.mapping_service import mapping_service

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=list[ToolRead])
async def list_tools(session: SessionDep, user: CurrentUser, _access: AgentsRead):
    return await mapping_service.list_org_tools(session, user.organization_id)


@router.get("/{tool_id}", response_model=ToolRead)
async def get_tool(tool_id: str, session: SessionDep, user: CurrentUser, _access: AgentsRead):
    tool = await session.get(Tool, tool_id)
    if tool is None:
        result = await session.execute(select(Tool).where(Tool.name == tool_id))
        tool = result.scalar_one_or_none()
    if tool is None or tool.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tool not found")
    return tool

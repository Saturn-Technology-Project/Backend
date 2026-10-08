from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.schemas import AppMcpRead, AppMcpWrite
from app.services.org_app_mcp_service import org_app_mcp_service
from app.services.org_app_state_service import org_app_state_service

router = APIRouter(prefix="/org-app-mcp", tags=["org-app-mcp"])


@router.get("", response_model=AppMcpRead)
async def read_org_app_mcp(session: SessionDep, user: CurrentUser):
    return await org_app_mcp_service.read(session, user.organization_id)


@router.put("", response_model=AppMcpRead)
async def replace_org_app_mcp(body: AppMcpWrite, session: SessionDep, user: CurrentUser):
    if not await org_app_state_service.can_write(session, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot change team agents")
    return await org_app_mcp_service.replace(session, user.organization_id, body.model_dump())

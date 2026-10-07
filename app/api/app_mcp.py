from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.schemas import AppMcpRead, AppMcpWrite
from app.services.app_mcp_service import app_mcp_service

router = APIRouter(prefix="/app-mcp", tags=["app-mcp"])


@router.get("", response_model=AppMcpRead)
async def read_app_mcp(session: SessionDep, user: CurrentUser):
    return await app_mcp_service.read(session, user.id)


@router.put("", response_model=AppMcpRead)
async def replace_app_mcp(body: AppMcpWrite, session: SessionDep, user: CurrentUser):
    return await app_mcp_service.replace(session, user.id, body.model_dump())

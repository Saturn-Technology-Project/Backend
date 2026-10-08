from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.schemas import AppStateRead, AppStateWrite
from app.services.app_state_service import app_state_service
from app.services.org_app_state_service import org_app_state_service

router = APIRouter(prefix="/app-state", tags=["app-state"])


@router.get("", response_model=AppStateRead)
async def read_app_state(session: SessionDep, user: CurrentUser):
    return await app_state_service.read(session, user.id)


@router.put("", response_model=AppStateRead)
async def replace_app_state(body: AppStateWrite, session: SessionDep, user: CurrentUser):
    team_ids = await org_app_state_service.agent_ids(session, user.organization_id)
    return await app_state_service.replace(session, user.id, body.model_dump(), team_ids)

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.schemas import OrgAppStateRead, OrgAppStateWrite
from app.services.org_app_state_service import org_app_state_service

router = APIRouter(prefix="/org-app-state", tags=["org-app-state"])


@router.get("", response_model=OrgAppStateRead)
async def read_org_app_state(session: SessionDep, user: CurrentUser):
    return await org_app_state_service.read(session, user.organization_id)


@router.put("", response_model=OrgAppStateRead)
async def replace_org_app_state(body: OrgAppStateWrite, session: SessionDep, user: CurrentUser):
    if not await org_app_state_service.can_write(session, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot change team agents")
    return await org_app_state_service.replace(session, user.organization_id, body.model_dump())

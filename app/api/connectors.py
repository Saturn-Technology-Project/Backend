from fastapi import APIRouter

from app.api.deps import AgentsRead, AgentsWrite, CurrentUser, SessionDep
from app.schemas import ConnectorEnrollResponse, ConnectorRead
from app.services.connector_service import connector_service, serialize_connector

router = APIRouter(prefix="/connectors", tags=["connectors"])


@router.get("", response_model=ConnectorRead)
async def get_connector(session: SessionDep, user: CurrentUser, _access: AgentsRead):
    connector = await connector_service.get_for_org(session, user.organization_id)
    return serialize_connector(connector, user.organization_id)


@router.get("/enrollment", response_model=ConnectorEnrollResponse)
async def get_enrollment(session: SessionDep, user: CurrentUser, _access: AgentsWrite):
    return await connector_service.get_enrollment(session, user.organization_id)


@router.post("/enroll", response_model=ConnectorEnrollResponse)
async def enroll_connector(session: SessionDep, user: CurrentUser, _access: AgentsWrite):
    return await connector_service.issue_token(session, user.organization_id)

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.agents import serialize_agent
from app.api.deps import CurrentUser, SessionDep
from app.api.mcp_servers import serialize_mcp_server
from app.models import Employee, User
from app.schemas import (
    AcceptInviteRequest,
    EmployeeRead,
    LoginRequest,
    LoginResponse,
    McpGatewayRead,
    OrgUserRead,
    UserRead,
)
from app.services.access import allows, empty_permissions, normalize_permissions
from app.services.organization_service import serialize_organization
from app.services.auth_service import auth_service
from app.services.mapping_service import mapping_service
from app.services.mcp_gateway import mcp_gateway
from app.services.member_service import member_service

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)


def serialize_org_user(employee: Employee) -> OrgUserRead:
    member = employee.user
    return OrgUserRead(
        id=employee.id,
        user_id=employee.user_id,
        email=member.email if member is not None else "",
        name=member.name if member is not None else "",
        title=employee.title,
        role=employee.role,
        status=employee.status,
    )


async def serialize_user(session, user: User) -> UserRead:
    org = serialize_organization(user.organization)
    employee = await mapping_service.employee_for_user(session, user.id)
    employee_read = None
    role = None
    permissions = empty_permissions()
    assigned_agent_ids: list[str] = []
    assigned_mcp_ids: list[str] = []
    users: list[OrgUserRead] = []
    if employee is not None:
        permissions = normalize_permissions(employee.permissions)
        role = employee.role
        employee_read = EmployeeRead(
            id=employee.id,
            title=employee.title,
            status=employee.status,
            role=employee.role,
            permissions=permissions,
        )
        assigned_agent_ids = [agent.id for agent in employee.agents]
        assigned_mcp_ids = [server.id for server in employee.mcp_servers]
        if allows(permissions, "team", "read"):
            members = await member_service.list_members(session, user.organization_id)
            users = [serialize_org_user(item) for item in members]

    agents = [
        serialize_agent(agent)
        for agent in await mapping_service.list_org_agents(session, user.organization_id)
    ]
    servers = [
        serialize_mcp_server(server)
        for server in await mapping_service.list_org_mcp_servers(session, user.organization_id)
    ]
    snapshot = mcp_gateway.snapshot(user.organization_id)
    return UserRead(
        id=user.id,
        email=user.email,
        name=user.name,
        status=user.status,
        is_platform_admin=user.is_platform_admin,
        organization=org,
        enterprise=org,
        employee=employee_read,
        agents=agents,
        mcp_servers=servers,
        users=users,
        assigned_agent_ids=assigned_agent_ids,
        assigned_mcp_server_ids=assigned_mcp_ids,
        mcp_gateway=McpGatewayRead.model_validate(snapshot),
        role=role,
        permissions=permissions,
    )


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, session: SessionDep):
    token, user = await auth_service.login(session, body.email, body.password)
    await mapping_service.bind_workspace(session, user)
    return LoginResponse(token=token, user=await serialize_user(session, user))


@router.post("/logout", status_code=204)
async def logout(
    session: SessionDep,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    if creds is not None:
        await auth_service.logout(session, creds.credentials)


@router.get("/me", response_model=UserRead)
async def me(user: CurrentUser, session: SessionDep):
    await mapping_service.bind_workspace(session, user)
    return await serialize_user(session, user)


@router.post("/accept-invite", response_model=LoginResponse)
async def accept_invite(body: AcceptInviteRequest, session: SessionDep):
    user = await member_service.accept_invite(session, body.token, body.password)
    await mapping_service.bind_workspace(session, user)
    token, user = await auth_service.issue_session(session, user)
    return LoginResponse(token=token, user=await serialize_user(session, user))

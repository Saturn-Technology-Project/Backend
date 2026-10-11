from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.features.agent.agents.controllers.agents import serialize_agent
from app.features.identity.auth.controllers.deps import CurrentUser, SessionDep
from app.features.identity.auth.controllers.session_cookie import COOKIE_NAME, clear_session_cookie, set_session_cookie
from app.features.mcp.servers.controllers.mcp_servers import serialize_mcp_server
from app.features.tooling.tool_sets.controllers.tool_sets import serialize_tool_set
from app.models import Employee, User
from app.schemas import (
    AcceptInviteRequest,
    EmployeeRead,
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    McpGatewayRead,
    OrgUserRead,
    PasswordChange,
    PhotoUpdate,
    ProfileUpdate,
    RegisterRequest,
    ResetPasswordRequest,
    UserRead,
)
from app.features.organization.org.services.access import allows, empty_permissions, normalize_permissions
from app.features.organization.org.services.organization_service import serialize_organization
from app.features.platform.admin.services.admin_service import admin_service
from app.features.identity.auth.services.auth_service import auth_service
from app.features.identity.auth.services.photo import clean_photo
from app.features.identity.auth.services.mailer import send_reset_code
from app.features.organization.org.services.mapping_service import mapping_service
from app.features.mcp.servers.services.mcp_gateway import mcp_gateway
from app.features.organization.org.services.member_service import member_service
from app.features.organization.org.services.plans import profile_identity
from app.features.tooling.tool_sets.services.tool_set_service import tool_set_service

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
    agent_edit_ids: list[str] | None = None
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
        agent_edit_ids = None if employee.agent_edit_ids is None else list(employee.agent_edit_ids)
        if allows(permissions, "team", "read"):
            members = await member_service.list_members(session, user.organization_id)
            users = [serialize_org_user(item) for item in members]

    agents = [
        serialize_agent(agent)
        for agent in await mapping_service.list_org_agents(session, user.organization_id)
        if str((agent.settings or {}).get("origin") or "") != "saturn_mcp"
    ]
    servers = [
        serialize_mcp_server(server)
        for server in await mapping_service.list_mcp_servers(session, user)
    ]
    tool_sets = [
        serialize_tool_set(item)
        for item in await tool_set_service.list_sets(session, user.organization_id)
    ]
    snapshot = mcp_gateway.snapshot(user.organization_id)
    return UserRead(
        id=user.id,
        email=user.email,
        name=user.name,
        photo=user.photo or None,
        status=user.status,
        is_platform_admin=user.is_platform_admin,
        organization=org,
        enterprise=org,
        employee=employee_read,
        agents=agents,
        mcp_servers=servers,
        tool_sets=tool_sets,
        users=users,
        assigned_agent_ids=assigned_agent_ids,
        assigned_mcp_server_ids=assigned_mcp_ids,
        agent_edit_ids=agent_edit_ids,
        mcp_gateway=McpGatewayRead.model_validate(snapshot),
        role=role,
        permissions=permissions,
        profile_id=profile_identity(
            user.id,
            user.organization_id,
            user.organization.kind,
            user.organization.plan,
        ),
    )


def _presented_token(request: Request, creds: HTTPAuthorizationCredentials | None) -> str:
    if creds is not None and creds.scheme.lower() == "bearer":
        return creds.credentials
    return request.cookies.get(COOKIE_NAME, "")


@router.post("/register", response_model=LoginResponse, status_code=201)
async def register(body: RegisterRequest, request: Request, response: Response, session: SessionDep):
    user = await admin_service.register_personal(
        session,
        name=body.name,
        email=body.email,
        password=body.password,
    )
    await mapping_service.bind_workspace(session, user)
    token, user = await auth_service.issue_session(session, user)
    set_session_cookie(response, request, token)
    return LoginResponse(token=token, user=await serialize_user(session, user))


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request, response: Response, session: SessionDep):
    token, user = await auth_service.login(session, body.email, body.password)
    await mapping_service.bind_workspace(session, user)
    set_session_cookie(response, request, token)
    return LoginResponse(token=token, user=await serialize_user(session, user))


@router.post("/session", status_code=204)
async def adopt_session(
    request: Request,
    response: Response,
    session: SessionDep,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    user = await auth_service.user_from_token(session, creds.credentials)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
    set_session_cookie(response, request, creds.credentials)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    session: SessionDep,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    token = _presented_token(request, creds)
    if token:
        await auth_service.logout(session, token)
    clear_session_cookie(response, request)


@router.get("/me", response_model=UserRead)
async def me(user: CurrentUser, session: SessionDep):
    await mapping_service.bind_workspace(session, user)
    return await serialize_user(session, user)


@router.patch("/me/photo", response_model=UserRead)
async def update_my_photo(body: PhotoUpdate, user: CurrentUser, session: SessionDep):
    user.photo = clean_photo(body.photo)
    await session.flush()
    await mapping_service.bind_workspace(session, user)
    return await serialize_user(session, user)


@router.patch("/me", response_model=UserRead)
async def update_me(body: ProfileUpdate, user: CurrentUser, session: SessionDep):
    await auth_service.update_profile(session, user, name=body.name, email=body.email)
    await mapping_service.bind_workspace(session, user)
    return await serialize_user(session, user)


@router.post("/accept-invite", response_model=LoginResponse)
async def accept_invite(body: AcceptInviteRequest, request: Request, response: Response, session: SessionDep):
    user = await member_service.accept_invite(session, body.token, body.password)
    await mapping_service.bind_workspace(session, user)
    token, user = await auth_service.issue_session(session, user)
    set_session_cookie(response, request, token)
    return LoginResponse(token=token, user=await serialize_user(session, user))


@router.post("/forgot-password", status_code=204)
async def forgot_password(body: ForgotPasswordRequest, session: SessionDep):
    code = await auth_service.request_password_reset(session, body.email)
    if not code:
        return
    try:
        await send_reset_code(body.email, code)
    except HTTPException:
        await auth_service.discard_password_reset(session, body.email)
        raise


@router.post("/reset-password", status_code=204)
async def reset_password(body: ResetPasswordRequest, session: SessionDep):
    await auth_service.reset_password(session, body.email, body.code, body.password)


@router.patch("/password", status_code=204)
async def change_password(
    body: PasswordChange,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    await auth_service.change_password(
        session,
        user,
        body.current_password,
        body.new_password,
        keep_token=_presented_token(request, creds) or None,
    )

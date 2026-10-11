from __future__ import annotations

from fastapi import APIRouter, status

from app.features.agent.agents.controllers.agents import serialize_agent
from app.features.identity.auth.controllers.deps import PlatformAdmin, SessionDep
from app.features.mcp.servers.controllers.mcp_servers import serialize_mcp_server
from app.features.organization.org.controllers.org import serialize_member
from app.models import AuditLog, User
from app.schemas import (
    AdminConnectionChannel,
    AdminEventRead,
    AdminHealthCheck,
    AdminMemberCreateRequest,
    AdminMcpSummary,
    AdminOrgConnections,
    AdminOrgDetail,
    AdminOrgOnboardRequest,
    AdminOrgSummary,
    AdminOrgUpdateRequest,
    AdminOverviewRead,
    AdminRuntimeRead,
    AdminServiceStatus,
    AdminSettingsRead,
    AdminStatsRead,
    AdminUsageRow,
    AdminUserRead,
    AgentRead,
    MemberPasswordResetRequest,
    MemberRead,
    MemberUpdateRequest,
)
from app.features.platform.admin.services.admin_service import admin_service
from app.features.execution.activity.services.audit_service import audit_service
from app.features.organization.org.services.member_service import member_service
from app.core.enums import ActorType

router = APIRouter(prefix="/admin", tags=["admin"])


def serialize_org(snap: dict) -> AdminOrgSummary:
    return AdminOrgSummary(**snap)


def serialize_event(item: AuditLog) -> AdminEventRead:
    org = item.organization
    return AdminEventRead(
        id=item.id,
        event=item.event,
        actor_type=item.actor_type,
        actor_id=item.actor_id,
        organization_id=item.organization_id,
        organization_name=org.name if org is not None else None,
        result=item.result,
        payload=item.payload,
        created_at=item.created_at,
    )


def serialize_admin_user(user: User) -> AdminUserRead:
    employee = user.employee
    org = user.organization
    return AdminUserRead(
        id=user.id,
        email=user.email,
        name=user.name,
        status=user.status,
        is_platform_admin=user.is_platform_admin,
        organization_id=user.organization_id,
        organization_name=org.name if org is not None else "",
        employee_id=employee.id if employee is not None else None,
        title=employee.title if employee is not None else None,
        role=employee.role if employee is not None else None,
        created_at=user.created_at,
    )


@router.get("/overview", response_model=AdminOverviewRead)
async def get_overview(session: SessionDep, _admin: PlatformAdmin):
    payload = await admin_service.overview(session)
    return AdminOverviewRead(
        stats=AdminStatsRead(**payload["stats"]),
        services=[AdminServiceStatus(**item) for item in payload["services"]],
        events=[serialize_event(item) for item in payload["events"]],
    )


@router.get("/stats", response_model=AdminStatsRead)
async def get_stats(session: SessionDep, _admin: PlatformAdmin):
    return AdminStatsRead(**await admin_service.stats(session))


@router.get("/orgs", response_model=list[AdminOrgSummary])
async def list_orgs(session: SessionDep, _admin: PlatformAdmin):
    return [serialize_org(item) for item in await admin_service.list_orgs(session)]


@router.post("/orgs", response_model=AdminOrgDetail, status_code=status.HTTP_201_CREATED)
async def onboard_org(body: AdminOrgOnboardRequest, session: SessionDep, admin: PlatformAdmin):
    org, employee = await admin_service.onboard_org(
        session,
        admin,
        name=body.name,
        admin_email=body.admin_email,
        admin_name=body.admin_name,
        admin_password=body.admin_password,
        admin_title=body.admin_title,
        plan=body.plan,
        kind=body.kind,
    )
    snap = await admin_service.org_snapshot(session, org)
    return AdminOrgDetail(
        **snap,
        members=[serialize_member(employee)],
        agents=[],
        health=AdminHealthCheck(**await admin_service.org_health(session, org)),
        mcp_servers=[],
    )


@router.get("/orgs/{organization_id}", response_model=AdminOrgDetail)
async def get_org(organization_id: str, session: SessionDep, _admin: PlatformAdmin):
    org = await admin_service.get_org(session, organization_id)
    snap = await admin_service.org_snapshot(session, org)
    members = await member_service.list_members(session, org.id)
    agents = await admin_service.list_agents(session, org.id)
    servers = await admin_service.list_mcp_servers(session, org.id)
    return AdminOrgDetail(
        **snap,
        members=[serialize_member(item) for item in members],
        agents=[serialize_agent(item) for item in agents],
        health=AdminHealthCheck(**await admin_service.org_health(session, org)),
        mcp_servers=[serialize_mcp_server(item) for item in servers],
    )


@router.patch("/orgs/{organization_id}", response_model=AdminOrgSummary)
async def update_org(
    organization_id: str,
    body: AdminOrgUpdateRequest,
    session: SessionDep,
    admin: PlatformAdmin,
):
    org = await admin_service.update_org(
        session,
        admin,
        organization_id,
        name=body.name,
        status=body.status,
        plan=body.plan,
        deployment_mode=body.deployment_mode,
        runtime_region=body.runtime_region,
    )
    return serialize_org(await admin_service.org_snapshot(session, org))


@router.get("/orgs/{organization_id}/members", response_model=list[MemberRead])
async def list_org_members(organization_id: str, session: SessionDep, _admin: PlatformAdmin):
    await admin_service.get_org(session, organization_id)
    members = await member_service.list_members(session, organization_id)
    return [serialize_member(item) for item in members]


@router.post(
    "/orgs/{organization_id}/members",
    response_model=MemberRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_org_member(
    organization_id: str,
    body: AdminMemberCreateRequest,
    session: SessionDep,
    admin: PlatformAdmin,
):
    org = await admin_service.get_org(session, organization_id)
    employee = await admin_service.create_member(
        session,
        organization=org,
        email=body.email,
        name=body.name,
        password=body.password,
        title=body.title,
        role=body.role,
        permissions=body.permissions.model_dump() if body.permissions is not None else None,
        status=body.status,
        agent_ids=body.agent_ids,
        mcp_server_ids=body.mcp_server_ids,
        actor=admin,
    )
    return serialize_member(employee)


@router.patch("/orgs/{organization_id}/members/{employee_id}", response_model=MemberRead)
async def update_org_member(
    organization_id: str,
    employee_id: str,
    body: MemberUpdateRequest,
    session: SessionDep,
    admin: PlatformAdmin,
):
    await admin_service.get_org(session, organization_id)
    employee = await member_service.update_member(
        session,
        admin,
        employee_id,
        name=body.name,
        title=body.title,
        role=body.role,
        permissions=body.permissions.model_dump() if body.permissions is not None else None,
        status=body.status,
        agent_ids=body.agent_ids,
        agent_edit_ids=body.agent_edit_ids,
        mcp_server_ids=body.mcp_server_ids,
        organization_id=organization_id,
    )
    await audit_service.log(
        session,
        event="member_updated",
        actor_type=ActorType.USER,
        actor_id=admin.id,
        organization_id=organization_id,
        payload={"employee_id": employee_id, "status": employee.status, "role": employee.role},
    )
    return serialize_member(employee)


@router.post("/orgs/{organization_id}/members/{employee_id}/password", response_model=MemberRead)
async def reset_org_member_password(
    organization_id: str,
    employee_id: str,
    body: MemberPasswordResetRequest,
    session: SessionDep,
    admin: PlatformAdmin,
):
    await admin_service.get_org(session, organization_id)
    employee = await member_service.reset_password(
        session,
        employee_id,
        body.password,
        organization_id=organization_id,
    )
    await audit_service.log(
        session,
        event="member_password_reset",
        actor_type=ActorType.USER,
        actor_id=admin.id,
        organization_id=organization_id,
        payload={"employee_id": employee_id},
    )
    return serialize_member(employee)


@router.get("/orgs/{organization_id}/agents", response_model=list[AgentRead])
async def list_org_agents(organization_id: str, session: SessionDep, _admin: PlatformAdmin):
    await admin_service.get_org(session, organization_id)
    agents = await admin_service.list_agents(session, organization_id)
    return [serialize_agent(item) for item in agents]


@router.get("/users", response_model=list[AdminUserRead])
async def list_users(session: SessionDep, _admin: PlatformAdmin):
    users = await admin_service.list_users(session)
    return [serialize_admin_user(user) for user in users]


@router.get("/connections", response_model=list[AdminOrgConnections])
async def list_connections(session: SessionDep, _admin: PlatformAdmin):
    rows = await admin_service.connections(session)
    return [
        AdminOrgConnections(
            organization_id=item["organization_id"],
            organization_name=item["organization_name"],
            channels=[AdminConnectionChannel(**channel) for channel in item["channels"]],
        )
        for item in rows
    ]


@router.get("/runtimes", response_model=list[AdminRuntimeRead])
async def list_runtimes(session: SessionDep, _admin: PlatformAdmin):
    return [AdminRuntimeRead(**item) for item in await admin_service.runtimes(session)]


@router.get("/mcp", response_model=AdminMcpSummary)
async def get_mcp(session: SessionDep, _admin: PlatformAdmin):
    return AdminMcpSummary(**await admin_service.mcp_summary(session))


@router.get("/usage", response_model=list[AdminUsageRow])
async def get_usage(session: SessionDep, _admin: PlatformAdmin):
    return [AdminUsageRow(**item) for item in await admin_service.usage(session)]


@router.get("/audit", response_model=list[AdminEventRead])
async def get_audit(session: SessionDep, _admin: PlatformAdmin):
    return [serialize_event(item) for item in await admin_service.list_events(session, limit=100)]


@router.get("/settings", response_model=AdminSettingsRead)
async def get_settings(session: SessionDep, _admin: PlatformAdmin):
    payload = await admin_service.settings(session)
    return AdminSettingsRead(
        platform=payload["platform"],
        security=payload["security"],
        infrastructure=payload["infrastructure"],
        admins=[serialize_admin_user(user) for user in payload["admins"]],
    )

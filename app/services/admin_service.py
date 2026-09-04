from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status as http_status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.ids import new_id
from app.models import (
    Action,
    Agent,
    AuditLog,
    Connector,
    Employee,
    McpServer,
    Organization,
    Payment,
    Policy,
    Run,
    Tool,
    User,
)
from app.models.enums import ActorType
from app.services.access import permissions_for_role, resolve_permissions
from app.services.agent_hub import agent_hub
from app.services.audit_service import audit_service
from app.services.auth_service import hash_password
from app.services.mcp_gateway import mcp_gateway
from app.services.member_service import member_service

settings = get_settings()
PLANS = {"trial", "starter", "enterprise"}
ORG_STATUSES = {"active", "trial", "suspended"}
DEPLOYMENTS = {"cloud", "customer_vpc", "on_prem"}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_heartbeat(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    text = str(value or "").strip()
    if not text:
        return None
    return _aware(datetime.fromisoformat(text.replace("Z", "+00:00")))


def _live_connector(organization_id: str) -> dict | None:
    return agent_hub.connector_snapshot(organization_id)


def _runtime_connected(organization_id: str) -> bool:
    live = _live_connector(organization_id)
    return bool(live and live.get("connected"))


class AdminService:
    async def stats(self, session: AsyncSession) -> dict[str, int]:
        organizations = await session.scalar(select(func.count()).select_from(Organization))
        users = await session.scalar(
            select(func.count()).select_from(User).where(User.status == "active")
        )
        agents = await session.scalar(select(func.count()).select_from(Agent))
        connections = await session.scalar(
            select(func.count()).select_from(McpServer).where(McpServer.status == "active")
        )
        return {
            "organizations": int(organizations or 0),
            "users": int(users or 0),
            "agents": int(agents or 0),
            "connections": int(connections or 0),
        }

    async def overview(self, session: AsyncSession) -> dict:
        stats = await self.stats(session)
        await self._bind_all_mcp(session)
        try:
            await session.scalar(select(func.count()).select_from(Organization))
            db_status = "operational"
        except Exception:
            db_status = "down"
        mcp_errors = 0
        runtime_connected = False
        for org in (await session.execute(select(Organization))).scalars():
            snap = mcp_gateway.snapshot(org.id)
            mcp_errors += max(0, int(snap["total"]) - int(snap["connected"]))
            if _runtime_connected(org.id):
                runtime_connected = True
        mcp_status = "degraded" if mcp_errors else "operational"
        events = await self.list_events(session, limit=12)
        return {
            "stats": stats,
            "services": [
                {"id": "api", "name": "API", "status": "operational"},
                {
                    "id": "runtime",
                    "name": "Agent Runtime",
                    "status": "operational" if runtime_connected else "down",
                },
                {"id": "gateway", "name": "MCP Gateway", "status": mcp_status},
                {"id": "database", "name": "Database", "status": db_status},
            ],
            "events": events,
        }

    async def _bind_all_mcp(self, session: AsyncSession) -> None:
        result = await session.execute(select(Organization.id))
        for org_id in result.scalars():
            await mcp_gateway.connect_organization(session, org_id)

    async def list_orgs(self, session: AsyncSession) -> list[dict]:
        await self._bind_all_mcp(session)
        result = await session.execute(select(Organization).order_by(Organization.created_at.asc()))
        orgs = list(result.scalars().all())
        rows = []
        for org in orgs:
            rows.append(await self.org_snapshot(session, org))
        return rows

    async def org_snapshot(self, session: AsyncSession, org: Organization) -> dict:
        user_count, agent_count = await self.org_counts(session, org.id)
        tool_count = int(
            await session.scalar(
                select(func.count()).select_from(Tool).where(Tool.organization_id == org.id)
            )
            or 0
        )
        mcp_count = int(
            await session.scalar(
                select(func.count()).select_from(McpServer).where(McpServer.organization_id == org.id)
            )
            or 0
        )
        connector = (
            await session.execute(select(Connector).where(Connector.organization_id == org.id))
        ).scalar_one_or_none()
        heartbeat = _parse_heartbeat(
            (_live_connector(org.id) or {}).get("last_heartbeat_at")
        ) or _aware(connector.last_seen_at if connector is not None else None)
        snap = mcp_gateway.snapshot(org.id)
        mcp_status = snap["status"] if int(snap["total"] or 0) else "none"
        if org.status == "suspended":
            runtime_status = "disconnected"
        elif _runtime_connected(org.id):
            runtime_status = "connected"
        else:
            runtime_status = "disconnected"
        return {
            "id": org.id,
            "name": org.name,
            "status": org.status,
            "plan": org.plan,
            "deployment_mode": org.deployment_mode,
            "runtime_region": org.runtime_region,
            "runtime_version": org.runtime_version,
            "created_at": org.created_at,
            "user_count": user_count,
            "agent_count": agent_count,
            "tool_count": tool_count,
            "connection_count": mcp_count,
            "runtime_status": runtime_status,
            "mcp_status": mcp_status,
            "last_heartbeat_at": heartbeat,
        }

    async def get_org(self, session: AsyncSession, organization_id: str) -> Organization:
        org = await session.get(Organization, organization_id)
        if org is None:
            raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Organization not found")
        return org

    async def org_counts(self, session: AsyncSession, organization_id: str) -> tuple[int, int]:
        users = await session.scalar(
            select(func.count()).select_from(User).where(User.organization_id == organization_id)
        )
        agents = await session.scalar(
            select(func.count()).select_from(Agent).where(Agent.organization_id == organization_id)
        )
        return int(users or 0), int(agents or 0)

    async def org_health(self, session: AsyncSession, org: Organization) -> dict:
        await mcp_gateway.connect_organization(session, org.id)
        snap = mcp_gateway.snapshot(org.id)
        summary = await self.org_snapshot(session, org)
        issues: list[str] = []
        agents = summary["agent_count"]
        policies = await session.scalar(
            select(func.count()).select_from(Policy).where(Policy.organization_id == org.id)
        )
        mcp = snap["status"] if int(snap["total"] or 0) else "none"
        checks = {
            "runtime": summary["runtime_status"],
            "mcp": mcp,
            "agents": "ok" if int(agents or 0) else "none",
            "policies": "ok" if int(policies or 0) else "none",
            "database": "ok",
        }
        if org.status == "suspended":
            issues.append("Organization is suspended")
        if checks["runtime"] != "connected":
            issues.append("Connector not connected")
        if int(snap["total"] or 0) > 0 and snap["status"] != "connected":
            missing = int(snap["total"]) - int(snap["connected"])
            if missing:
                issues.append(f"{missing} MCP server{'s' if missing != 1 else ''} unreachable")
        score = 100
        if checks["runtime"] != "connected":
            score -= 40
        if checks["mcp"] == "degraded":
            score -= 15
        if checks["mcp"] == "disconnected":
            score -= 25
        return {**checks, "score": max(score, 0), "issues": issues}

    async def list_agents(self, session: AsyncSession, organization_id: str) -> list[Agent]:
        result = await session.execute(
            select(Agent)
            .where(Agent.organization_id == organization_id)
            .order_by(Agent.created_at.asc())
        )
        return list(result.scalars().all())

    async def list_mcp_servers(self, session: AsyncSession, organization_id: str) -> list[McpServer]:
        result = await session.execute(
            select(McpServer)
            .options(selectinload(McpServer.tools))
            .where(McpServer.organization_id == organization_id)
            .order_by(McpServer.name.asc())
        )
        return list(result.scalars().unique().all())

    async def onboard_org(
        self,
        session: AsyncSession,
        actor: User,
        *,
        name: str,
        admin_email: str,
        admin_name: str,
        admin_password: str,
        admin_title: str,
        plan: str = "trial",
    ) -> tuple[Organization, Employee]:
        cleaned_name = name.strip()
        if not cleaned_name:
            raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "Organization name is required")
        cleaned_plan = (plan or "trial").strip().lower()
        if cleaned_plan not in PLANS:
            raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "Unknown plan")
        employee = await self.create_member(
            session,
            organization=None,
            org_name=cleaned_name,
            plan=cleaned_plan,
            email=admin_email,
            name=admin_name,
            password=admin_password,
            title=admin_title.strip() or "Admin",
            role="admin",
            permissions=None,
            status="active",
            agent_ids=[],
            mcp_server_ids=[],
        )
        org = await session.get(Organization, employee.organization_id)
        if org is None:
            raise HTTPException(http_status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to create organization")
        await audit_service.log(
            session,
            event="org_onboarded",
            actor_type=ActorType.USER,
            actor_id=actor.id,
            organization_id=org.id,
            payload={"name": org.name, "plan": org.plan},
        )
        return org, employee

    async def update_org(
        self,
        session: AsyncSession,
        actor: User,
        organization_id: str,
        *,
        name: str | None,
        status: str | None = None,
        plan: str | None = None,
        deployment_mode: str | None = None,
        runtime_region: str | None = None,
    ) -> Organization:
        org = await self.get_org(session, organization_id)
        changed: dict[str, str] = {}
        if name is not None:
            cleaned = name.strip()
            if not cleaned:
                raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "Organization name is required")
            org.name = cleaned
            changed["name"] = cleaned
        if status is not None:
            cleaned = status.strip().lower()
            if cleaned not in ORG_STATUSES:
                raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "Status must be active, trial, or suspended")
            org.status = cleaned
            changed["status"] = cleaned
        if plan is not None:
            cleaned = plan.strip().lower()
            if cleaned not in PLANS:
                raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "Unknown plan")
            org.plan = cleaned
            changed["plan"] = cleaned
        if deployment_mode is not None:
            cleaned = deployment_mode.strip().lower()
            if cleaned not in DEPLOYMENTS:
                raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "Unknown deployment mode")
            org.deployment_mode = cleaned
            changed["deployment_mode"] = cleaned
        if runtime_region is not None:
            cleaned = runtime_region.strip()
            if cleaned:
                org.runtime_region = cleaned
                changed["runtime_region"] = cleaned
        await session.flush()
        if changed:
            await audit_service.log(
                session,
                event="org_updated",
                actor_type=ActorType.USER,
                actor_id=actor.id,
                organization_id=org.id,
                payload=changed,
            )
        return org

    async def list_users(self, session: AsyncSession) -> list[User]:
        result = await session.execute(
            select(User)
            .options(selectinload(User.organization), selectinload(User.employee))
            .order_by(User.created_at.asc())
        )
        return list(result.scalars().unique().all())

    async def list_events(self, session: AsyncSession, limit: int = 50) -> list[AuditLog]:
        result = await session.execute(
            select(AuditLog)
            .options(selectinload(AuditLog.organization))
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().unique().all())

    async def connections(self, session: AsyncSession) -> list[dict]:
        rows = []
        for snap in await self.list_orgs(session):
            org = await self.get_org(session, snap["id"])
            live = _live_connector(org.id)
            heartbeat = _parse_heartbeat((live or {}).get("last_heartbeat_at")) or snap[
                "last_heartbeat_at"
            ]
            runtime_ok = snap["runtime_status"] == "connected"
            connector_ok = bool(live and live.get("connected"))
            gateway = mcp_gateway.snapshot(org.id)
            rows.append(
                {
                    "organization_id": org.id,
                    "organization_name": org.name,
                    "channels": [
                        {
                            "id": "runtime",
                            "name": "Agent Runtime",
                            "status": "connected" if runtime_ok else "disconnected",
                            "last_heartbeat_at": heartbeat if runtime_ok else None,
                            "version": org.runtime_version if runtime_ok else None,
                        },
                        {
                            "id": "gateway",
                            "name": "MCP Gateway",
                            "status": gateway["status"] if int(gateway["total"] or 0) else "none",
                            "last_heartbeat_at": heartbeat if connector_ok else None,
                            "detail": f"{gateway['connected']}/{gateway['total']} servers",
                        },
                        {
                            "id": "connector",
                            "name": "Connector",
                            "status": "connected" if connector_ok else "disconnected",
                            "version": org.runtime_version if connector_ok else None,
                            "last_heartbeat_at": heartbeat if connector_ok else None,
                        },
                    ],
                }
            )
        return rows

    async def runtimes(self, session: AsyncSession) -> list[dict]:
        running = (
            select(Run.organization_id, func.count(Run.id).label("active"))
            .where(Run.status.in_(("queued", "running", "waiting_approval")))
            .group_by(Run.organization_id)
        )
        active_map = {row[0]: int(row[1]) for row in (await session.execute(running)).all()}
        agents = list(
            (
                await session.execute(
                    select(Agent).where(Agent.runtime_id.isnot(None)).order_by(Agent.created_at.asc())
                )
            ).scalars().all()
        )
        grouped: dict[tuple[str, str], list[Agent]] = {}
        for agent in agents:
            runtime_id = str(agent.runtime_id or "").strip()
            if not runtime_id:
                continue
            grouped.setdefault((agent.organization_id, runtime_id), []).append(agent)
        orgs = {
            org.id: org
            for org in (await session.execute(select(Organization))).scalars().all()
        }
        rows = []
        for (organization_id, runtime_id), members in grouped.items():
            org = orgs.get(organization_id)
            if org is None:
                continue
            live = _live_connector(organization_id)
            member_ids = {agent.id for agent in members}
            live_ids = set((live or {}).get("agent_ids") or [])
            connected = bool(live and live.get("connected") and (member_ids & live_ids))
            last_failed = (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.organization_id == org.id,
                        AuditLog.event.in_(("run_failed", "x402_failed")),
                    )
                    .order_by(AuditLog.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            heartbeat = _parse_heartbeat((live or {}).get("last_heartbeat_at"))
            if heartbeat is None:
                seen = [_aware(agent.last_seen_at) for agent in members]
                seen = [item for item in seen if item is not None]
                heartbeat = max(seen) if seen else None
            rows.append(
                {
                    "id": runtime_id,
                    "organization_id": org.id,
                    "organization_name": org.name,
                    "status": "healthy" if connected else "disconnected",
                    "region": org.runtime_region,
                    "version": org.runtime_version,
                    "deployment_mode": org.deployment_mode,
                    "active_runs": active_map.get(org.id, 0),
                    "agent_count": len(members),
                    "last_heartbeat_at": heartbeat,
                    "last_error": last_failed.result if last_failed is not None else None,
                }
            )
        rows.sort(key=lambda item: (item["organization_name"], item["id"]))
        return rows

    async def mcp_summary(self, session: AsyncSession) -> dict:
        await self._bind_all_mcp(session)
        result = await session.execute(
            select(McpServer)
            .options(selectinload(McpServer.tools), selectinload(McpServer.organization))
            .order_by(McpServer.name.asc())
        )
        servers = list(result.scalars().unique().all())
        items = []
        healthy = 0
        errors = 0
        tools = 0
        for server in servers:
            link = mcp_gateway.link_for(server.id)
            tool_count = len(server.tools or [])
            tools += tool_count
            connected = bool(link and link.connected)
            if connected:
                healthy += 1
            else:
                errors += 1
            items.append(
                {
                    "id": server.id,
                    "name": server.name,
                    "organization_id": server.organization_id,
                    "organization_name": server.organization.name if server.organization else "",
                    "status": "connected" if connected else "error",
                    "tools": tool_count,
                    "endpoint": server.endpoint,
                    "error": None if connected else (link.error if link else "Disconnected"),
                }
            )
        return {
            "servers": len(servers),
            "tools": tools,
            "healthy": healthy,
            "errors": errors,
            "items": items,
        }

    async def usage(self, session: AsyncSession) -> list[dict]:
        run_rows = {
            row[0]: int(row[1])
            for row in (
                await session.execute(
                    select(Run.organization_id, func.count(Run.id)).group_by(Run.organization_id)
                )
            ).all()
        }
        call_rows = {
            row[0]: int(row[1])
            for row in (
                await session.execute(
                    select(Run.organization_id, func.count(Action.id))
                    .join(Action, Action.run_id == Run.id)
                    .group_by(Run.organization_id)
                )
            ).all()
        }
        pay_rows = {
            row[0]: (int(row[1]), str(row[2] or 0))
            for row in (
                await session.execute(
                    select(Run.organization_id, func.count(Payment.id), func.coalesce(func.sum(Payment.amount), 0))
                    .select_from(Payment)
                    .join(Run, Run.id == Payment.run_id)
                    .group_by(Run.organization_id)
                )
            ).all()
        }
        orgs = (await session.execute(select(Organization).order_by(Organization.name.asc()))).scalars()
        rows = []
        for org in orgs:
            count, volume = pay_rows.get(org.id, (0, "0"))
            rows.append(
                {
                    "organization_id": org.id,
                    "organization_name": org.name,
                    "plan": org.plan,
                    "runs": run_rows.get(org.id, 0),
                    "tool_calls": call_rows.get(org.id, 0),
                    "payments": count,
                    "payment_volume": volume,
                }
            )
        return rows

    async def settings(self, session: AsyncSession) -> dict:
        admins = [user for user in await self.list_users(session) if user.is_platform_admin]
        return {
            "platform": {
                "name": "Saturn",
                "version": "0.1.0",
                "default_org_id": settings.default_org_id,
                "session_ttl_days": settings.session_ttl_days,
            },
            "security": {
                "authentication": "session",
                "admin_count": len(admins),
                "audit": "enabled",
            },
            "infrastructure": {
                "runtime": "in-process",
                "mcp": "mcp-gateway",
                "x402_network": settings.x402_network,
                "secrets": [
                    {"id": "database_url", "kind": "dsn", "configured": True},
                    {"id": "redis_url", "kind": "url", "configured": True},
                    {"id": "x402_pay_to", "kind": "address", "configured": True},
                ],
            },
            "admins": admins,
        }

    async def create_member(
        self,
        session: AsyncSession,
        *,
        organization: Organization | None,
        org_name: str | None = None,
        plan: str = "trial",
        email: str,
        name: str,
        password: str,
        title: str,
        role: str,
        permissions: dict | None,
        status: str,
        agent_ids: list[str],
        mcp_server_ids: list[str],
        actor: User | None = None,
    ) -> Employee:
        if len(password) < 8:
            raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "Password must be at least 8 characters")
        normalized = email.strip().lower()
        if not normalized:
            raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "Email is required")
        cleaned_name = name.strip()
        if not cleaned_name:
            raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "Name is required")

        existing = await session.execute(select(User).where(User.email == normalized))
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(http_status.HTTP_409_CONFLICT, "Email is already used")

        cleaned_status = (status or "active").strip().lower()
        if cleaned_status not in {"active", "disabled"}:
            raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "Status must be active or disabled")

        resolved_role, resolved_perms = resolve_permissions(role, permissions)

        if organization is None:
            organization = Organization(
                id=new_id("org"),
                name=(org_name or "").strip() or "Organization",
                status="trial" if plan == "trial" else "active",
                plan=plan if plan in PLANS else "trial",
            )
            session.add(organization)
            await session.flush()

        await member_service._validate_ids(session, organization.id, agent_ids, mcp_server_ids)

        user = User(
            id=new_id("user"),
            organization_id=organization.id,
            email=normalized,
            name=cleaned_name,
            password_hash=hash_password(password),
            status=cleaned_status,
            is_platform_admin=False,
        )
        session.add(user)
        await session.flush()
        employee = Employee(
            id=new_id("emp"),
            organization_id=organization.id,
            user_id=user.id,
            title=title.strip() or "Operator",
            role=resolved_role,
            permissions=resolved_perms or permissions_for_role(resolved_role),
            status=cleaned_status,
        )
        session.add(employee)
        await session.flush()
        loaded = await member_service.load_member(session, employee.id)
        if loaded is None:
            raise HTTPException(http_status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to create member")
        await member_service._set_assignments(
            session,
            loaded,
            organization_id=organization.id,
            agent_ids=agent_ids,
            mcp_server_ids=mcp_server_ids,
        )
        created = await member_service.load_member(session, loaded.id)
        assert created is not None
        if actor is not None:
            await audit_service.log(
                session,
                event="member_created",
                actor_type=ActorType.USER,
                actor_id=actor.id,
                organization_id=organization.id,
                payload={"email": created.user.email if created.user else normalized, "role": created.role},
            )
        return created


admin_service = AdminService()

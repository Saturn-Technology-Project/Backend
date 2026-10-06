from __future__ import annotations

import secrets
from datetime import timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ids import new_id, utcnow
from app.models import Agent, Employee, McpServer, Organization, User
from app.models.invitation import Invitation
from app.models.session import AuthSession
from app.services.access import is_org_admin, permissions_for_role, resolve_permissions
from app.services.auth_service import hash_password, hash_token

INVITE_TTL_DAYS = 7


class MemberService:
    async def load_member(self, session: AsyncSession, employee_id: str) -> Employee | None:
        result = await session.execute(
            select(Employee)
            .options(
                selectinload(Employee.user),
                selectinload(Employee.agents),
                selectinload(Employee.mcp_servers),
                selectinload(Employee.organization),
            )
            .where(Employee.id == employee_id)
        )
        return result.scalar_one_or_none()

    async def list_members(self, session: AsyncSession, organization_id: str) -> list[Employee]:
        result = await session.execute(
            select(Employee)
            .options(
                selectinload(Employee.user),
                selectinload(Employee.agents),
                selectinload(Employee.mcp_servers),
            )
            .where(Employee.organization_id == organization_id)
            .order_by(Employee.created_at.asc())
        )
        return list(result.scalars().unique().all())

    async def list_invites(self, session: AsyncSession, organization_id: str) -> list[Invitation]:
        result = await session.execute(
            select(Invitation)
            .where(Invitation.organization_id == organization_id)
            .order_by(Invitation.created_at.desc())
        )
        return list(result.scalars().all())

    async def require_org_member(
        self, session: AsyncSession, organization_id: str, employee_id: str
    ) -> Employee:
        employee = await self.load_member(session, employee_id)
        if employee is None or employee.organization_id != organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
        return employee

    async def _admin_count(self, session: AsyncSession, organization_id: str) -> int:
        result = await session.execute(
            select(Employee).where(
                Employee.organization_id == organization_id,
                Employee.status == "active",
            )
        )
        return sum(1 for item in result.scalars() if is_org_admin(item.permissions))

    async def _set_assignments(
        self,
        session: AsyncSession,
        employee: Employee,
        *,
        organization_id: str,
        agent_ids: list[str] | None,
        mcp_server_ids: list[str] | None,
    ) -> None:
        if agent_ids is not None:
            agents = []
            for agent_id in agent_ids:
                agent = await session.get(Agent, agent_id)
                if agent is None or agent.organization_id != organization_id:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, f"Agent not found: {agent_id}")
                agents.append(agent)
            employee.agents = agents
        if mcp_server_ids is not None:
            servers = []
            for server_id in mcp_server_ids:
                server = await session.get(McpServer, server_id)
                if server is None or server.organization_id != organization_id:
                    raise HTTPException(
                        status.HTTP_404_NOT_FOUND, f"MCP server not found: {server_id}"
                    )
                servers.append(server)
            employee.mcp_servers = servers
        await session.flush()

    async def invite(
        self,
        session: AsyncSession,
        actor: User,
        *,
        email: str,
        name: str,
        title: str,
        role: str,
        permissions: dict | None,
        agent_ids: list[str] | None,
        mcp_server_ids: list[str] | None,
    ) -> tuple[Invitation, str]:
        organization_id = actor.organization_id
        normalized = email.strip().lower()
        if not normalized:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email is required")

        existing_user = await session.execute(select(User).where(User.email == normalized))
        user = existing_user.scalar_one_or_none()
        if user is not None:
            if user.organization_id == organization_id:
                raise HTTPException(status.HTTP_409_CONFLICT, "User is already a member of this org")
            raise HTTPException(status.HTTP_409_CONFLICT, "Email is already used in another org")

        pending = await session.execute(
            select(Invitation).where(
                Invitation.organization_id == organization_id,
                Invitation.email == normalized,
                Invitation.status == "pending",
            )
        )
        if pending.scalar_one_or_none() is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "A pending invite already exists")

        resolved_role, resolved_perms = resolve_permissions(role, permissions)
        await self._validate_ids(session, organization_id, agent_ids, mcp_server_ids)

        raw = "inv_" + secrets.token_urlsafe(24)
        invite = Invitation(
            id=new_id("invite"),
            organization_id=organization_id,
            invited_by_user_id=actor.id,
            email=normalized,
            name=name.strip() or normalized,
            title=title.strip() or "Operator",
            role=resolved_role,
            permissions=resolved_perms,
            agent_ids=list(agent_ids or []),
            mcp_server_ids=list(mcp_server_ids or []),
            token_hash=hash_token(raw),
            expires_at=utcnow() + timedelta(days=INVITE_TTL_DAYS),
            status="pending",
        )
        session.add(invite)
        await session.flush()
        return invite, raw

    async def _validate_ids(
        self,
        session: AsyncSession,
        organization_id: str,
        agent_ids: list[str] | None,
        mcp_server_ids: list[str] | None,
    ) -> None:
        for agent_id in agent_ids or []:
            agent = await session.get(Agent, agent_id)
            if agent is None or agent.organization_id != organization_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"Agent not found: {agent_id}")
        for server_id in mcp_server_ids or []:
            server = await session.get(McpServer, server_id)
            if server is None or server.organization_id != organization_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"MCP server not found: {server_id}")

    async def revoke_invite(
        self, session: AsyncSession, organization_id: str, invite_id: str
    ) -> Invitation:
        invite = await session.get(Invitation, invite_id)
        if invite is None or invite.organization_id != organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")
        if invite.status != "pending":
            raise HTTPException(status.HTTP_409_CONFLICT, "Invite is not pending")
        invite.status = "revoked"
        await session.flush()
        return invite

    async def accept_invite(self, session: AsyncSession, token: str, password: str) -> User:
        if len(password) < 8:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password must be at least 8 characters")
        result = await session.execute(
            select(Invitation).where(Invitation.token_hash == hash_token(token))
        )
        invite = result.scalar_one_or_none()
        if invite is None or invite.status != "pending":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired invite")
        expires = invite.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= utcnow():
            invite.status = "expired"
            await session.flush()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired invite")

        org = await session.get(Organization, invite.organization_id)
        if org is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Organization no longer exists")

        existing = await session.execute(select(User).where(User.email == invite.email))
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "User already exists")

        user = User(
            id=new_id("user"),
            organization_id=org.id,
            email=invite.email,
            name=invite.name,
            password_hash=hash_password(password),
            status="active",
        )
        session.add(user)
        await session.flush()
        employee = Employee(
            id=new_id("emp"),
            organization_id=org.id,
            user_id=user.id,
            title=invite.title,
            role=invite.role,
            permissions=invite.permissions or permissions_for_role(invite.role),
            status="active",
        )
        session.add(employee)
        await session.flush()
        employee = await self.load_member(session, employee.id)
        if employee is None:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to create member")
        await self._set_assignments(
            session,
            employee,
            organization_id=org.id,
            agent_ids=list(invite.agent_ids or []),
            mcp_server_ids=list(invite.mcp_server_ids or []),
        )
        invite.status = "accepted"
        invite.accepted_at = utcnow()
        invite.accepted_user_id = user.id
        await session.flush()
        result = await session.execute(
            select(User).options(selectinload(User.organization)).where(User.id == user.id)
        )
        return result.scalar_one()

    async def update_member(
        self,
        session: AsyncSession,
        actor: User,
        employee_id: str,
        *,
        name: str | None,
        title: str | None,
        role: str | None,
        permissions: dict | None,
        status: str | None,
        agent_ids: list[str] | None,
        mcp_server_ids: list[str] | None,
        agent_edit_ids: list[str] | None = None,
        organization_id: str | None = None,
    ) -> Employee:
        from fastapi import status as http_status

        org_id = organization_id or actor.organization_id
        employee = await self.require_org_member(session, org_id, employee_id)
        next_role = employee.role
        next_perms = dict(employee.permissions or {})
        if role is not None or permissions is not None:
            next_role, next_perms = resolve_permissions(
                role or employee.role,
                permissions,
            )

        next_status = employee.status
        if status is not None:
            cleaned = status.strip().lower()
            if cleaned not in {"active", "disabled"}:
                raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "Status must be active or disabled")
            next_status = cleaned

        losing_admin = is_org_admin(employee.permissions) and (
            next_status != "active" or not is_org_admin(next_perms)
        )
        if losing_admin:
            remaining = await self._admin_count(session, org_id)
            if remaining <= 1:
                raise HTTPException(
                    http_status.HTTP_409_CONFLICT,
                    "Organization must keep at least one admin",
                )

        if name is not None and employee.user is not None:
            employee.user.name = name.strip() or employee.user.name
        if title is not None:
            employee.title = title.strip() or employee.title
        employee.role = next_role
        employee.permissions = next_perms
        if next_status != employee.status:
            employee.status = next_status
            if employee.user is not None:
                employee.user.status = "active" if next_status == "active" else "disabled"
                if next_status == "disabled":
                    await self._revoke_sessions(session, employee.user_id)
        await self._set_assignments(
            session,
            employee,
            organization_id=org_id,
            agent_ids=agent_ids,
            mcp_server_ids=mcp_server_ids,
        )
        if agent_edit_ids is not None:
            assigned = {agent.id for agent in employee.agents}
            employee.agent_edit_ids = [agent_id for agent_id in agent_edit_ids if agent_id in assigned]
        await session.flush()
        loaded = await self.load_member(session, employee.id)
        assert loaded is not None
        return loaded

    async def reset_password(
        self,
        session: AsyncSession,
        employee_id: str,
        password: str,
        *,
        organization_id: str,
    ) -> Employee:
        if len(password) < 8:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password must be at least 8 characters")
        employee = await self.require_org_member(session, organization_id, employee_id)
        if employee.user is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "User account not found")
        employee.user.password_hash = hash_password(password)
        await self._revoke_sessions(session, employee.user_id)
        await session.flush()
        loaded = await self.load_member(session, employee.id)
        assert loaded is not None
        return loaded

    async def _revoke_sessions(self, session: AsyncSession, user_id: str) -> None:
        result = await session.execute(
            select(AuthSession).where(
                AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
            )
        )
        now = utcnow()
        for record in result.scalars():
            record.revoked_at = now
        await session.flush()


member_service = MemberService()

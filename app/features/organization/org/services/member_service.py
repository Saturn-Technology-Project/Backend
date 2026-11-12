from __future__ import annotations

import logging
import secrets
from datetime import timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ids import new_id, utcnow
from app.models import Agent, Employee, McpServer, Organization, User
from app.features.organization.org.models.invitation import Invitation
from app.features.identity.auth.models.session import AuthSession
from app.features.organization.org.services.access import (
    access_reduced,
    is_org_admin,
    permissions_for_role,
    resolve_permissions,
)
from app.features.identity.auth.models.password_reset import PasswordReset
from app.features.identity.auth.services import mailer
from app.features.identity.auth.services.auth_service import hash_password, hash_token
from app.shared import runtime

logger = logging.getLogger(__name__)

INVITE_TTL_DAYS = 7
RESET_TTL_MINUTES = 10
MIN_PASSWORD_LENGTH = 8
# One message for every "this address cannot be invited here" case, so the response does not
# say whether the address belongs to a member or to a pending invite.
INVITE_CONFLICT = "This email already has a member or a pending invite in this organization"


def require_password_policy(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
        )


def lock_org_employees_statement(organization_id: str):
    return (
        select(Employee)
        .where(Employee.organization_id == organization_id)
        .order_by(Employee.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def unusable_password_hash() -> str:
    """A hash of a random secret nobody knows. The account can only be entered after a reset."""
    return hash_password(secrets.token_urlsafe(32))


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

    async def list_members(
        self,
        session: AsyncSession,
        organization_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Employee]:
        statement = (
            select(Employee)
            .options(
                selectinload(Employee.user),
                selectinload(Employee.agents),
                selectinload(Employee.mcp_servers),
            )
            .where(Employee.organization_id == organization_id)
            .order_by(Employee.created_at.asc(), Employee.id.asc())
        )
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        result = await session.execute(statement)
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

    async def _lock_org_employees(self, session: AsyncSession, organization_id: str) -> None:
        """Lock every employee row of the org until commit (Postgres; a no-op on SQLite).

        The last-admin check reads all admins and then writes one of them. Two concurrent
        demotions must not both pass it, so each takes these row locks first; the second waits
        for the first to commit and then reads the rows as the first left them.
        """
        await session.execute(lock_org_employees_statement(organization_id))

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
        agents, servers = await self._resolve_assignments(
            session, organization_id=organization_id, agent_ids=agent_ids, mcp_server_ids=mcp_server_ids
        )
        if agents is not None:
            employee.agents = agents
        if servers is not None:
            employee.mcp_servers = servers
            employee.mcp_assignments_initialized = True
        await session.flush()

    async def _resolve_assignments(
        self,
        session: AsyncSession,
        *,
        organization_id: str,
        agent_ids: list[str] | None,
        mcp_server_ids: list[str] | None,
    ) -> tuple[list[Agent] | None, list[McpServer] | None]:
        """Look every id up. Raises 404 without changing anything."""
        agents: list[Agent] | None = None
        if agent_ids is not None:
            agents = []
            for agent_id in agent_ids:
                agent = await session.get(Agent, agent_id)
                if agent is None or agent.organization_id != organization_id:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, f"Agent not found: {agent_id}")
                agents.append(agent)
        servers: list[McpServer] | None = None
        if mcp_server_ids is not None:
            servers = []
            for server_id in mcp_server_ids:
                server = await session.get(McpServer, server_id)
                if server is None or server.organization_id != organization_id:
                    raise HTTPException(
                        status.HTTP_404_NOT_FOUND, f"MCP server not found: {server_id}"
                    )
                servers.append(server)
        return agents, servers

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
    ) -> tuple[Invitation, str | None]:
        """Create an invite and email its one-time link.

        The token is returned to the caller only in local development and tests. In production
        it reaches the invitee by email only, so an admin cannot accept an invite in someone
        else's name.
        """
        organization_id = actor.organization_id
        org = await session.get(Organization, organization_id)
        if org is not None and org.kind != "organization":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Personal accounts cannot add members")
        normalized = email.strip().lower()
        if not normalized:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email is required")

        dev = runtime.local_dev_mode()
        if not dev and not runtime.email_configured():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Email delivery is not configured")

        existing_user = await session.execute(select(User).where(User.email == normalized))
        user = existing_user.scalar_one_or_none()
        if user is not None and user.organization_id == organization_id:
            raise HTTPException(status.HTTP_409_CONFLICT, INVITE_CONFLICT)
        # An address that belongs to another organization gets the same response as a new one,
        # so invites cannot be used to learn which emails have Saturn accounts elsewhere. Such an
        # invite cannot be accepted (accept_invite refuses existing users) and no email is sent.
        deliverable = user is None

        pending = await session.execute(
            select(Invitation).where(
                Invitation.organization_id == organization_id,
                Invitation.email == normalized,
                Invitation.status == "pending",
            )
        )
        if pending.scalar_one_or_none() is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, INVITE_CONFLICT)

        resolved_role, resolved_perms = resolve_permissions(role, permissions)
        await self._validate_ids(session, organization_id, agent_ids, mcp_server_ids)

        raw = "inv_" + secrets.token_urlsafe(24)
        if deliverable and runtime.email_configured():
            # Send before anything is written: a failed send raises and leaves no dangling invite.
            await mailer.send_invite(
                normalized,
                raw,
                organization=org.name if org is not None else "",
                inviter=actor.name or "",
                days=INVITE_TTL_DAYS,
                locale=getattr(actor, "locale", None),
            )
        elif not deliverable:
            logger.info("Invite created for an address that already has an account; no email sent")
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
        return invite, (raw if dev else None)

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
        require_password_policy(password)
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
            # No servers on the invite: the first workspace bind assigns the org's servers once.
            mcp_server_ids=list(invite.mcp_server_ids or []) or None,
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
        if role is not None or permissions is not None or status is not None:
            await self._lock_org_employees(session, org_id)
        employee = await self.require_org_member(session, org_id, employee_id)
        previous_perms = dict(employee.permissions or {})
        previous_agents = {agent.id for agent in employee.agents}
        previous_mcp = {server.id for server in employee.mcp_servers}
        previous_edit = set(employee.agent_edit_ids or [])
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
            # Disabling a member disables the whole user, so an org admin must not be able
            # to lock a platform admin out of the platform.
            if (
                next_status == "disabled"
                and employee.user is not None
                and employee.user.is_platform_admin
                and not actor.is_platform_admin
            ):
                raise HTTPException(http_status.HTTP_403_FORBIDDEN, "A platform admin cannot be disabled here")

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

        # Checked before the role or status changes below. A refused request is still committed,
        # so a 404 here after those changes would save them without revoking the member's tokens.
        await self._resolve_assignments(
            session, organization_id=org_id, agent_ids=agent_ids, mcp_server_ids=mcp_server_ids
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
        reduced = (
            access_reduced(previous_perms, next_perms)
            or next_status == "disabled"
            or (agent_ids is not None and bool(previous_agents - set(agent_ids)))
            or (mcp_server_ids is not None and bool(previous_mcp - set(mcp_server_ids)))
            or (agent_edit_ids is not None and bool(previous_edit - set(agent_edit_ids)))
        )
        if reduced and employee.user_id:
            await self._revoke_oauth(session, employee.user_id)
        loaded = await self.load_member(session, employee.id)
        assert loaded is not None
        return loaded

    async def issue_password_reset(self, session: AsyncSession, user: User) -> None:
        """Email the user a one-time reset code. Raises before writing anything if it cannot be sent."""
        if not runtime.email_configured():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Email delivery is not configured")
        code = f"{secrets.randbelow(1_000_000):06d}"
        await mailer.send_reset_code(user.email, code, user.locale)
        await self.store_reset_code(session, user.email, code)

    async def store_reset_code(self, session: AsyncSession, email: str, code: str) -> None:
        now = utcnow()
        open_codes = await session.execute(
            select(PasswordReset).where(
                PasswordReset.email == email,
                PasswordReset.used_at.is_(None),
            )
        )
        for record in open_codes.scalars():
            record.used_at = now
        session.add(
            PasswordReset(
                id=new_id("rst"),
                email=email,
                code_hash=hash_password(code),
                expires_at=now + timedelta(minutes=RESET_TTL_MINUTES),
                attempts=0,
            )
        )
        await session.flush()

    async def reset_password(
        self,
        session: AsyncSession,
        employee_id: str,
        *,
        organization_id: str,
    ) -> Employee:
        """Platform-admin reset: the admin never chooses or learns the new password.

        The current password stops working, every session and OAuth grant is revoked, and the
        user gets a one-time code by email to choose a new password with /auth/reset-password.
        """
        employee = await self.require_org_member(session, organization_id, employee_id)
        if employee.user is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "User account not found")
        await self.issue_password_reset(session, employee.user)
        employee.user.password_hash = unusable_password_hash()
        await self._revoke_sessions(session, employee.user_id)
        await self._revoke_oauth(session, employee.user_id)
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

    async def _revoke_oauth(self, session: AsyncSession, user_id: str) -> None:
        from app.features.identity.oauth.services.oauth_service import oauth_service

        await oauth_service.revoke_user_tokens(session, user_id)


member_service = MemberService()

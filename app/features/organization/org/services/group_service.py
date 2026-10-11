from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import ActorType, AuditEvent
from app.core.ids import new_id, utcnow
from app.features.execution.activity.services.audit_service import audit_service
from app.features.organization.org.models.group import Group, GroupMember
from app.features.organization.org.services.plans import GROUPS_REQUIRED, allows_groups
from app.models import Employee, Organization


class GroupService:
    async def list_groups(self, session: AsyncSession, organization_id: str) -> list[Group]:
        result = await session.execute(
            select(Group)
            .options(selectinload(Group.members))
            .where(Group.organization_id == organization_id)
            .order_by(Group.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_group(self, session: AsyncSession, organization_id: str, group_id: str) -> Group:
        result = await session.execute(
            select(Group).options(selectinload(Group.members)).where(Group.id == group_id)
        )
        group = result.scalar_one_or_none()
        if group is None or group.organization_id != organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
        return group

    async def create_group(
        self,
        session: AsyncSession,
        organization_id: str,
        *,
        actor_id: str,
        name: str,
        description: str = "",
    ) -> Group:
        await self._require_plan(session, organization_id)
        cleaned = name.strip()
        if not cleaned:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Group name is required")
        group = Group(
            id=new_id("grp"),
            organization_id=organization_id,
            name=cleaned,
            description=description.strip(),
            status="active",
        )
        session.add(group)
        await session.flush()
        await audit_service.log(
            session,
            event=AuditEvent.GROUP_UPDATED,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            organization_id=organization_id,
            result="created",
            payload={"group_id": group.id, "name": group.name},
        )
        return group

    async def update_group(
        self,
        session: AsyncSession,
        organization_id: str,
        group_id: str,
        *,
        actor_id: str,
        name: str | None = None,
        description: str | None = None,
        status_name: str | None = None,
    ) -> Group:
        group = await self.get_group(session, organization_id, group_id)
        if name is not None:
            cleaned = name.strip()
            if not cleaned:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Group name is required")
            group.name = cleaned
        if description is not None:
            group.description = description.strip()
        if status_name is not None:
            cleaned_status = status_name.strip().lower()
            if cleaned_status not in {"active", "disabled"}:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Status must be active or disabled")
            group.status = cleaned_status
        group.updated_at = utcnow()
        await session.flush()
        await audit_service.log(
            session,
            event=AuditEvent.GROUP_UPDATED,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            organization_id=organization_id,
            result="updated",
            payload={"group_id": group.id, "status": group.status},
        )
        return group

    async def add_member(
        self,
        session: AsyncSession,
        organization_id: str,
        group_id: str,
        employee_id: str,
        *,
        actor_id: str,
    ) -> Group:
        group = await self.get_group(session, organization_id, group_id)
        employee = await session.get(Employee, employee_id)
        if employee is None or employee.organization_id != organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
        if employee.status != "active":
            raise HTTPException(status.HTTP_409_CONFLICT, "Member is not active")
        existing = await session.scalar(
            select(GroupMember).where(
                GroupMember.group_id == group.id,
                GroupMember.employee_id == employee.id,
            )
        )
        if existing is None:
            session.add(GroupMember(id=new_id("gmem"), group_id=group.id, employee_id=employee.id))
            await session.flush()
            await audit_service.log(
                session,
                event=AuditEvent.GROUP_UPDATED,
                actor_type=ActorType.USER,
                actor_id=actor_id,
                organization_id=organization_id,
                result="member_added",
                payload={"group_id": group.id, "employee_id": employee.id},
            )
        return await self.get_group(session, organization_id, group.id)

    async def remove_member(
        self,
        session: AsyncSession,
        organization_id: str,
        group_id: str,
        employee_id: str,
        *,
        actor_id: str,
    ) -> Group:
        group = await self.get_group(session, organization_id, group_id)
        member = await session.scalar(
            select(GroupMember).where(
                GroupMember.group_id == group.id,
                GroupMember.employee_id == employee_id,
            )
        )
        if member is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Member is not in this group")
        await session.delete(member)
        await session.flush()
        await audit_service.log(
            session,
            event=AuditEvent.GROUP_UPDATED,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            organization_id=organization_id,
            result="member_removed",
            payload={"group_id": group.id, "employee_id": employee_id},
        )
        return await self.get_group(session, organization_id, group.id)

    async def _require_plan(self, session: AsyncSession, organization_id: str) -> None:
        org = await session.get(Organization, organization_id)
        if org is None or not allows_groups(org.plan):
            raise HTTPException(status.HTTP_403_FORBIDDEN, GROUPS_REQUIRED)


group_service = GroupService()

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import ActorType, AuditEvent
from app.core.ids import new_id, utcnow
from app.features.execution.activity.services.audit_service import audit_service
from app.features.execution.approvals.models.approval import ApprovalPolicy, ApprovalRequirement
from app.features.organization.org.models.group import Group
from app.features.organization.org.services.plans import GROUPS_REQUIRED, allows_groups
from app.models import Organization


class ApprovalPolicyService:
    async def list_policies(self, session: AsyncSession, organization_id: str) -> list[ApprovalPolicy]:
        result = await session.execute(
            select(ApprovalPolicy)
            .options(selectinload(ApprovalPolicy.requirements).selectinload(ApprovalRequirement.group))
            .where(ApprovalPolicy.organization_id == organization_id)
            .order_by(ApprovalPolicy.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_policy(
        self, session: AsyncSession, organization_id: str, policy_id: str
    ) -> ApprovalPolicy:
        result = await session.execute(
            select(ApprovalPolicy)
            .options(selectinload(ApprovalPolicy.requirements).selectinload(ApprovalRequirement.group))
            .where(ApprovalPolicy.id == policy_id)
        )
        policy = result.scalar_one_or_none()
        if policy is None or policy.organization_id != organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval policy not found")
        return policy

    async def create_policy(
        self,
        session: AsyncSession,
        organization_id: str,
        *,
        actor_id: str,
        name: str,
        required_count: int = 1,
        group_id: str | None = None,
        requirements: list[dict] | None = None,
        allow_requester: bool = True,
        expires_minutes: int | None = None,
    ) -> ApprovalPolicy:
        cleaned = name.strip()
        if not cleaned:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name is required")
        if requirements and group_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Choose one group or a list of groups",
            )
        if requirements:
            await self._require_capability(session, organization_id, count=2, group_id=None, grouped=True)
        grouped = await self._clean_requirements(session, organization_id, requirements)
        if grouped:
            count = sum(item[1] for item in grouped)
        else:
            count = int(required_count)
            if count < 1 or count > 20:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Required approvals must be from 1 to 20")
        if expires_minutes is not None and expires_minutes < 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Expiration must be at least one minute")
        await self._require_capability(
            session,
            organization_id,
            count=count,
            group_id=None if grouped else group_id,
            grouped=bool(grouped),
        )
        if group_id and not grouped:
            await self._group(session, organization_id, group_id)
        policy = ApprovalPolicy(
            id=new_id("apol"),
            organization_id=organization_id,
            name=cleaned,
            status="active",
            required_count=count,
            group_id=None if grouped else group_id,
            allow_requester=allow_requester,
            expires_minutes=expires_minutes,
            version=1,
        )
        session.add(policy)
        await session.flush()
        for index, (requirement_group_id, requirement_count) in enumerate(grouped):
            session.add(
                ApprovalRequirement(
                    id=new_id("areq"),
                    approval_policy_id=policy.id,
                    group_id=requirement_group_id,
                    required_count=requirement_count,
                    position=index,
                )
            )
        await session.flush()
        await audit_service.log(
            session,
            event=AuditEvent.GROUP_UPDATED,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            organization_id=organization_id,
            result="approval_policy_created",
            payload={
                "approval_policy_id": policy.id,
                "required_count": policy.required_count,
                "group_id": policy.group_id,
                "requirements": [
                    {"group_id": group_id, "required_count": requirement_count}
                    for group_id, requirement_count in grouped
                ],
                "allow_requester": policy.allow_requester,
            },
        )
        session.expire(policy, ["requirements"])
        return await self.get_policy(session, organization_id, policy.id)

    async def update_policy(
        self,
        session: AsyncSession,
        organization_id: str,
        policy_id: str,
        *,
        actor_id: str,
        name: str | None = None,
        status_name: str | None = None,
        required_count: int | None = None,
        group_id: str | None = None,
        requirements: list[dict] | None = None,
        clear_group: bool = False,
        allow_requester: bool | None = None,
        expires_minutes: int | None = None,
    ) -> ApprovalPolicy:
        policy = await self.get_policy(session, organization_id, policy_id)
        if name is not None:
            cleaned = name.strip()
            if not cleaned:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name is required")
            policy.name = cleaned
        if status_name is not None:
            cleaned_status = status_name.strip().lower()
            if cleaned_status not in {"active", "disabled"}:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Status must be active or disabled")
            policy.status = cleaned_status
        if required_count is not None:
            count = int(required_count)
            if count < 1 or count > 20:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Required approvals must be from 1 to 20")
            policy.required_count = count
        if requirements is not None:
            if requirements and (group_id or clear_group):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Choose one group or a list of groups",
                )
            grouped = await self._clean_requirements(session, organization_id, requirements)
            policy.requirements.clear()
            await session.flush()
            for index, (requirement_group_id, requirement_count) in enumerate(grouped):
                policy.requirements.append(
                    ApprovalRequirement(
                        id=new_id("areq"),
                        approval_policy_id=policy.id,
                        group_id=requirement_group_id,
                        required_count=requirement_count,
                        position=index,
                    )
                )
            if grouped:
                policy.group_id = None
                policy.required_count = sum(item[1] for item in grouped)
        elif clear_group:
            policy.group_id = None
        elif group_id is not None:
            await self._group(session, organization_id, group_id)
            policy.group_id = group_id
        if allow_requester is not None:
            policy.allow_requester = allow_requester
        if expires_minutes is not None:
            if expires_minutes < 1:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Expiration must be at least one minute")
            policy.expires_minutes = expires_minutes
        await self._require_capability(
            session,
            organization_id,
            count=policy.required_count,
            group_id=policy.group_id,
            grouped=bool(policy.requirements),
        )
        policy.version = (policy.version or 1) + 1
        policy.updated_at = utcnow()
        await session.flush()
        await audit_service.log(
            session,
            event=AuditEvent.GROUP_UPDATED,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            organization_id=organization_id,
            result="approval_policy_updated",
            payload={
                "approval_policy_id": policy.id,
                "version": policy.version,
                "required_count": policy.required_count,
                "status": policy.status,
            },
        )
        session.expire(policy, ["requirements"])
        return await self.get_policy(session, organization_id, policy.id)

    async def _clean_requirements(
        self,
        session: AsyncSession,
        organization_id: str,
        requirements: list[dict] | None,
    ) -> list[tuple[str, int]]:
        if not requirements:
            return []
        if len(requirements) > 10:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "An approval policy can include at most 10 groups")
        seen: set[str] = set()
        cleaned: list[tuple[str, int]] = []
        for item in requirements:
            group_id = str(item.get("group_id") or "").strip()
            count = int(item.get("required_count") or 0)
            if not group_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Each requirement needs a group")
            if count < 1 or count > 20:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Required approvals must be from 1 to 20")
            if group_id in seen:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Each group can appear once")
            seen.add(group_id)
            await self._group(session, organization_id, group_id)
            cleaned.append((group_id, count))
        return cleaned

    async def _group(self, session: AsyncSession, organization_id: str, group_id: str) -> Group:
        group = await session.get(Group, group_id)
        if group is None or group.organization_id != organization_id or group.status != "active":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found")
        return group

    async def _require_capability(
        self,
        session: AsyncSession,
        organization_id: str,
        *,
        count: int,
        group_id: str | None,
        grouped: bool = False,
    ) -> None:
        if count <= 1 and not group_id and not grouped:
            return
        org = await session.get(Organization, organization_id)
        if org is None or not allows_groups(org.plan):
            raise HTTPException(status.HTTP_403_FORBIDDEN, GROUPS_REQUIRED)


approval_policy_service = ApprovalPolicyService()

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ids import new_id, utcnow
from app.models import Agent, Employee, Organization, Tool, ToolSet, ToolSetItem, User
from app.features.organization.org.services.access import allows, may_edit_agent
from app.features.organization.org.services.plans import profile_identity, tool_set_limit


class ToolSetService:
    async def list_sets(
        self,
        session: AsyncSession,
        organization_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ToolSet]:
        statement = (
            select(ToolSet)
            .options(selectinload(ToolSet.items).selectinload(ToolSetItem.tool).selectinload(Tool.mcp_server))
            .where(ToolSet.organization_id == organization_id)
            .order_by(ToolSet.name.asc(), ToolSet.id.asc())
        )
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        result = await session.execute(statement)
        return list(result.scalars().unique().all())

    async def create(
        self,
        session: AsyncSession,
        user: User,
        *,
        name: str,
        description: str,
        items: list[dict],
    ) -> ToolSet:
        label = name.strip()
        if not label:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Tool set name is required")
        org = await session.get(Organization, user.organization_id)
        limit = tool_set_limit(org.plan if org is not None else None)
        if limit is not None:
            count = await session.scalar(
                select(func.count()).select_from(ToolSet).where(ToolSet.organization_id == user.organization_id)
            )
            if int(count or 0) >= limit:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Tool set limit reached")
        existing = await session.scalar(
            select(ToolSet).where(
                ToolSet.organization_id == user.organization_id,
                ToolSet.name == label,
            )
        )
        if existing is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "A tool set with this name already exists")
        tool_set = ToolSet(
            id=new_id("set"),
            organization_id=user.organization_id,
            name=label,
            description=description.strip(),
        )
        session.add(tool_set)
        await session.flush()
        await self._replace_items(session, user, tool_set, items)
        return await self._load(session, tool_set.id)

    async def update(
        self,
        session: AsyncSession,
        user: User,
        tool_set_id: str,
        *,
        name: str | None,
        description: str | None,
        items: list[dict] | None,
    ) -> ToolSet:
        tool_set = await self._owned(session, user, tool_set_id)
        if name is not None:
            label = name.strip()
            if not label:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Tool set name is required")
            clash = await session.scalar(
                select(ToolSet).where(
                    ToolSet.organization_id == user.organization_id,
                    ToolSet.name == label,
                    ToolSet.id != tool_set.id,
                )
            )
            if clash is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, "A tool set with this name already exists")
            tool_set.name = label
        if description is not None:
            tool_set.description = description.strip()
        tool_set.updated_at = utcnow()
        if items is not None:
            await self._replace_items(session, user, tool_set, items)
        await session.flush()
        return await self._load(session, tool_set.id)

    async def delete(self, session: AsyncSession, user: User, tool_set_id: str) -> None:
        tool_set = await self._owned(session, user, tool_set_id)
        agent_ids = list(
            await session.scalars(select(Agent.id).where(Agent.tool_set_id == tool_set.id))
        )
        await session.execute(
            update(Agent).where(Agent.tool_set_id == tool_set.id).values(tool_set_id=None)
        )
        await session.delete(tool_set)
        await session.flush()
        await self._notify_tools(agent_ids)

    def owns_profile(self, user: User, agent: Agent) -> bool:
        if str((agent.settings or {}).get("origin") or "") != "saturn_mcp":
            return False
        org = user.organization
        expected = profile_identity(
            user.id,
            user.organization_id,
            getattr(org, "kind", None),
            getattr(org, "plan", None),
        )
        return str((agent.settings or {}).get("profile_id") or "") == expected

    async def assign(
        self,
        session: AsyncSession,
        user: User,
        employee: Employee,
        agent: Agent,
        tool_set_id: str | None,
    ) -> None:
        if not allows(employee.permissions, "team", "write") and not (
            self.owns_profile(user, agent) or may_edit_agent(employee, agent.id)
        ):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot assign a tool set to this agent")
        previous = agent.tool_set_id
        if tool_set_id:
            await self._owned(session, user, tool_set_id)
            agent.tool_set_id = tool_set_id
        else:
            agent.tool_set_id = None
        if agent.tool_set_id != previous:
            await self._notify_tools([agent.id])

    async def _replace_items(self, session: AsyncSession, user: User, tool_set: ToolSet, items: list[dict]) -> None:
        wanted: dict[str, bool] = {}
        for item in items:
            tool_id = str(item.get("tool_id") or "").strip()
            if not tool_id or tool_id in wanted:
                continue
            tool = await session.get(Tool, tool_id)
            if tool is None or tool.organization_id != user.organization_id or tool.status != "active":
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Tool is not available")
            wanted[tool_id] = bool(item.get("approval"))
        if not wanted:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select at least one tool")
        existing = list(
            (await session.execute(select(ToolSetItem).where(ToolSetItem.tool_set_id == tool_set.id))).scalars().all()
        )
        for row in existing:
            if row.tool_id not in wanted:
                await session.delete(row)
            else:
                row.approval = wanted.pop(row.tool_id)
        for tool_id, approval in wanted.items():
            session.add(
                ToolSetItem(
                    id=new_id("item"),
                    tool_set_id=tool_set.id,
                    tool_id=tool_id,
                    approval=approval,
                )
            )
        await session.flush()
        agent_ids = list(
            await session.scalars(select(Agent.id).where(Agent.tool_set_id == tool_set.id))
        )
        await self._notify_tools(agent_ids)

    async def _notify_tools(self, agent_ids: list[str]) -> None:
        from app.features.mcp.saturn.services.mcp_sessions import mcp_sessions

        await mcp_sessions.notify(agent_ids)

    async def _owned(self, session: AsyncSession, user: User, tool_set_id: str) -> ToolSet:
        tool_set = await session.get(ToolSet, tool_set_id)
        if tool_set is None or tool_set.organization_id != user.organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Tool set not found")
        return tool_set

    async def _load(self, session: AsyncSession, tool_set_id: str) -> ToolSet:
        result = await session.execute(
            select(ToolSet)
            .options(selectinload(ToolSet.items).selectinload(ToolSetItem.tool).selectinload(Tool.mcp_server))
            .where(ToolSet.id == tool_set_id)
        )
        return result.scalar_one()


tool_set_service = ToolSetService()

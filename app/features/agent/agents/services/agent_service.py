from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.models import Agent, Employee, User
from app.core.enums import ActorType, AuditEvent
from app.schemas import AgentUpdate
from app.features.organization.org.services.access import allows, may_edit_agent
from app.features.agent.agents.services.agent_config import default_instructions, merge_settings
from app.features.execution.activity.services.audit_service import audit_service
from app.features.organization.org.services.mapping_service import mapping_service


class AgentService:
    async def list_agents(
        self, session: AsyncSession, user: User, *, limit: int | None = None, offset: int = 0
    ) -> list[Agent]:
        return await mapping_service.list_org_agents(
            session, user.organization_id, limit=limit, offset=offset
        )

    async def get_agent(self, session: AsyncSession, user: User, agent_id: str) -> Agent:
        return await mapping_service.require_org_agent(session, user, agent_id)

    async def create_agent(
        self,
        session: AsyncSession,
        user: User,
        *,
        name: str,
        description: str,
        model: str,
        agent_id: str | None = None,
    ) -> Agent:
        # The id is always generated here. A client-chosen or name-derived id is a global
        # primary key, so probing it would tell one tenant which ids exist in other tenants.
        agent = Agent(
            id=new_id("agent"),
            organization_id=user.organization_id,
            name=name,
            description=description,
            model=model,
            settings={"instructions": default_instructions(name, description)},
        )
        session.add(agent)
        await session.flush()
        await mapping_service.assign_agent(session, user, agent)
        return agent

    async def update_agent(
        self,
        session: AsyncSession,
        user: User,
        employee: Employee,
        agent_id: str,
        body: AgentUpdate,
    ) -> Agent:
        agent = await mapping_service.require_org_agent(session, user, agent_id)
        from app.features.tooling.tool_sets.services.tool_set_service import tool_set_service

        set_only = body.model_fields_set == {"tool_set_id"}
        if not may_edit_agent(employee, agent.id) and not (set_only and tool_set_service.owns_profile(user, agent)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot modify this agent")
        admin = allows(employee.permissions, "team", "write")
        if body.runtime_id is not None and not admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
        previous_prompt = str((agent.settings or {}).get("instructions") or "")
        if body.name is not None:
            name = body.name.strip()
            if name:
                agent.name = name
        if body.description is not None:
            agent.description = body.description
        if body.runtime_id is not None:
            agent.runtime_id = body.runtime_id.strip() or None
        if body.settings is not None:
            agent.settings = merge_settings(agent.settings, body.settings, admin=admin)
            model = str((agent.settings or {}).get("model") or "").strip()
            if model:
                agent.model = model
            next_prompt = str((agent.settings or {}).get("instructions") or "")
            if "instructions" in body.settings and next_prompt != previous_prompt:
                await audit_service.log(
                    session,
                    event=AuditEvent.PROMPT_UPDATED,
                    actor_type=ActorType.USER,
                    actor_id=user.id,
                    organization_id=user.organization_id,
                    result=agent.name,
                    payload={
                        "agent_id": agent.id,
                        "previous": previous_prompt,
                        "prompt": next_prompt,
                    },
                )
        if "tool_set_id" in body.model_fields_set:
            await tool_set_service.assign(session, user, employee, agent, body.tool_set_id)
        if body.model is not None:
            agent.model = body.model
            settings = dict(agent.settings or {})
            settings["model"] = body.model
            agent.settings = settings
        await session.flush()
        return agent


agent_service = AgentService()

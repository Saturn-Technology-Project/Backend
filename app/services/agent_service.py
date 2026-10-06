from __future__ import annotations

import secrets

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import public_ws_url
from app.core.ids import new_id, utcnow
from app.models import Agent, Employee, User
from app.models.enums import ActorType, AgentStatus, AuditEvent
from app.schemas import AgentEnrollResponse, AgentUpdate
from app.services.access import allows, may_edit_agent
from app.services.agent_config import default_instructions, merge_settings
from app.services.audit_service import audit_service
from app.services.auth_service import hash_token
from app.services.mapping_service import mapping_service
from app.services.wallet_service import wallet_service


def enrollment_payload(agent_id: str, token: str) -> AgentEnrollResponse:
    return AgentEnrollResponse(
        agent_id=agent_id,
        token=token,
        ws_url=public_ws_url(f"/ws/agents?agent_id={agent_id}&token={token}"),
    )


def _slug_id(prefix: str, name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return f"{prefix}_{slug}" if slug else new_id(prefix)


class AgentService:
    async def list_agents(self, session: AsyncSession, user: User) -> list[Agent]:
        return await mapping_service.list_org_agents(session, user.organization_id)

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
        ident = agent_id or _slug_id("agent", name)
        existing = await session.get(Agent, ident)
        if existing is not None:
            ident = new_id("agent")
        agent = Agent(
            id=ident,
            organization_id=user.organization_id,
            name=name,
            description=description,
            model=model,
            status=AgentStatus.ACTIVE,
            settings={"instructions": default_instructions(name, description)},
        )
        session.add(agent)
        await session.flush()
        await wallet_service.ensure_wallet(session, agent)
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
        if not may_edit_agent(employee, agent.id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot modify this agent")
        admin = allows(employee.permissions, "team", "write")
        if (
            body.runtime_id is not None
            or body.daily_spend_limit is not None
            or body.per_tx_limit is not None
        ) and not admin:
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
        if body.model is not None:
            agent.model = body.model
            settings = dict(agent.settings or {})
            settings["model"] = body.model
            agent.settings = settings
        if body.daily_spend_limit is not None or body.per_tx_limit is not None:
            wallet = await wallet_service.ensure_wallet(session, agent)
            wallet_service.update_limits(
                wallet,
                daily=body.daily_spend_limit,
                per_tx=body.per_tx_limit,
            )
        await session.flush()
        return agent

    async def issue_token(self, session: AsyncSession, user: User, agent_id: str) -> AgentEnrollResponse:
        agent = await mapping_service.require_org_agent(session, user, agent_id)
        raw = "agt_" + secrets.token_urlsafe(32)
        agent.token_hash = hash_token(raw)
        agent.enrollment_token = raw
        agent.last_seen_at = utcnow()
        await session.flush()
        return enrollment_payload(agent.id, raw)

    async def get_enrollment(
        self, session: AsyncSession, user: User, agent_id: str
    ) -> AgentEnrollResponse:
        agent = await mapping_service.require_org_agent(session, user, agent_id)
        if not agent.enrollment_token:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not enrolled")
        return enrollment_payload(agent.id, agent.enrollment_token)


agent_service = AgentService()

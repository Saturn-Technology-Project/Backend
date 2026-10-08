from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from app.api.deps import (
    AgentsRead,
    AgentsWrite,
    CurrentUser,
    SessionDep,
    TeamWrite,
)
from app.schemas import (
    AgentCreate,
    AgentRead,
    AgentUpdate,
    ConversationMessageRead,
    ConversationMessageWrite,
)
from app.services.agent_config import public_settings
from app.services.agent_service import agent_service
from app.services.conversation_service import conversation_service
from app.services.mapping_service import mapping_service
from app.services.removal_service import removal_service
from app.services.wallet_service import wallet_service

router = APIRouter(prefix="/agents", tags=["agents"])


def _recent_mcp_client(agent) -> bool:
    if str((agent.settings or {}).get("origin") or "") != "saturn_mcp":
        return False
    seen = agent.last_seen_at
    if seen is None:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen).total_seconds() < 120


def serialize_agent(agent, pushed: bool | None = None) -> AgentRead:
    return AgentRead(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        model=agent.model,
        organization_id=agent.organization_id,
        connected=_recent_mcp_client(agent),
        enrolled=str((agent.settings or {}).get("origin") or "") == "saturn_mcp",
        runtime_id=agent.runtime_id,
        last_seen_at=agent.last_seen_at,
        settings=public_settings(agent),
        tool_set_id=agent.tool_set_id,
        profile_id=str((agent.settings or {}).get("profile_id") or "") or None,
        origin=str((agent.settings or {}).get("origin") or "") or None,
        pushed=pushed,
    )


@router.get("", response_model=list[AgentRead])
async def list_agents(session: SessionDep, user: CurrentUser, _access: AgentsRead):
    await mapping_service.bind_workspace(session, user)
    agents = await agent_service.list_agents(session, user)
    return [serialize_agent(agent) for agent in agents]


def serialize_message(message) -> ConversationMessageRead:
    return ConversationMessageRead(
        id=message.id,
        agent_id=message.agent_id,
        input=message.input,
        output=message.output or "",
        status=message.status,
        actions=list(message.actions or []),
        error=message.error,
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


@router.get("/{agent_id}/messages", response_model=list[ConversationMessageRead])
async def list_messages(agent_id: str, session: SessionDep, user: CurrentUser, _access: AgentsRead):
    messages = await conversation_service.list_messages(session, user, agent_id)
    return [serialize_message(message) for message in messages]


@router.post("/{agent_id}/messages", response_model=ConversationMessageRead, status_code=201)
async def create_message(
    agent_id: str,
    body: ConversationMessageWrite,
    session: SessionDep,
    user: CurrentUser,
    _access: AgentsRead,
):
    message = await conversation_service.create_message(session, user, agent_id, body.input)
    return serialize_message(message)


@router.put("/{agent_id}/messages/{message_id}", response_model=ConversationMessageRead)
async def upsert_message(
    agent_id: str,
    message_id: str,
    body: ConversationMessageWrite,
    session: SessionDep,
    user: CurrentUser,
    _access: AgentsRead,
):
    message = await conversation_service.upsert_message(
        session,
        user,
        agent_id,
        message_id,
        text=body.input,
        output=body.output,
        run_status=body.status,
        actions=body.actions,
        error=body.error,
    )
    return serialize_message(message)


@router.get("/{agent_id}", response_model=AgentRead)
async def get_agent(agent_id: str, session: SessionDep, user: CurrentUser, _access: AgentsRead):
    agent = await agent_service.get_agent(session, user, agent_id)
    return serialize_agent(agent)


@router.post("", response_model=AgentRead, status_code=201)
async def create_agent(body: AgentCreate, session: SessionDep, user: CurrentUser, _access: AgentsWrite):
    agent = await agent_service.create_agent(
        session,
        user,
        name=body.name,
        description=body.description,
        model=body.model,
        agent_id=body.id,
    )
    return serialize_agent(agent)


@router.patch("/{agent_id}", response_model=AgentRead)
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    session: SessionDep,
    user: CurrentUser,
    employee: AgentsWrite,
):
    agent = await agent_service.update_agent(session, user, employee, agent_id, body)
    await wallet_service.ensure_wallet(session, agent)
    return serialize_agent(agent, pushed=False)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, session: SessionDep, user: CurrentUser, _access: TeamWrite):
    await removal_service.delete_agent(session, user, agent_id)

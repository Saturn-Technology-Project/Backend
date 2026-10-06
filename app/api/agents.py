from __future__ import annotations

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
    AgentEnrollResponse,
    AgentRead,
    AgentUpdate,
    ConversationMessageRead,
    ConversationMessageWrite,
)
from app.services.agent_config import command_payload, public_settings
from app.services.agent_hub import agent_hub
from app.services.agent_service import agent_service
from app.services.conversation_service import conversation_service
from app.services.mapping_service import mapping_service
from app.services.removal_service import removal_service
from app.services.wallet_service import wallet_service

router = APIRouter(prefix="/agents", tags=["agents"])


def serialize_agent(agent, pushed: bool | None = None) -> AgentRead:
    return AgentRead(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        model=agent.model,
        status=agent.status,
        organization_id=agent.organization_id,
        connected=agent_hub.is_connected(agent.id),
        enrolled=bool(agent.enrollment_token or agent.token_hash or agent.connector_id),
        connector_id=agent.connector_id,
        runtime_id=agent.runtime_id or agent_hub.runtime_id(agent.id) or None,
        last_seen_at=agent_hub.last_heartbeat(agent.id)
        or agent_hub.last_seen(agent.id)
        or agent.last_seen_at,
        settings=public_settings(agent),
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
    wallet = await wallet_service.ensure_wallet(session, agent)
    pushed = await agent_hub.send_command(
        agent.id, "config.update", command_payload(agent, wallet)
    )
    return serialize_agent(agent, pushed=pushed)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, session: SessionDep, user: CurrentUser, _access: TeamWrite):
    await removal_service.delete_agent(session, user, agent_id)


@router.get("/{agent_id}/enrollment", response_model=AgentEnrollResponse)
async def get_enrollment(
    agent_id: str, session: SessionDep, user: CurrentUser, _access: AgentsWrite
):
    return await agent_service.get_enrollment(session, user, agent_id)


@router.post("/{agent_id}/enroll", response_model=AgentEnrollResponse)
async def enroll_agent(
    agent_id: str, session: SessionDep, user: CurrentUser, _access: AgentsWrite
):
    return await agent_service.issue_token(session, user, agent_id)

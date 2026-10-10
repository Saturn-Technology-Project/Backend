from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import select

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
from app.models import OAuthAccess, OAuthClient, User
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


def _profile_user_id(agent) -> str:
    settings = agent.settings or {}
    stored = str(settings.get("registered_user_id") or "").strip()
    if stored:
        return stored
    profile = str(settings.get("profile_id") or "")
    prefix = f"{agent.organization_id}:"
    if profile.startswith(prefix):
        return profile[len(prefix) :]
    return profile


async def _registrations(session, agents) -> dict[str, dict[str, str]]:
    user_ids = {user_id for agent in agents if (user_id := _profile_user_id(agent))}
    users: dict[str, User] = {}
    if user_ids:
        rows = await session.execute(select(User).where(User.id.in_(user_ids)))
        users = {item.id: item for item in rows.scalars().all()}
    missing = [
        agent
        for agent in agents
        if not str((agent.settings or {}).get("client_id") or "").strip() and agent.name and _profile_user_id(agent)
    ]
    clients: dict[tuple[str, str], str] = {}
    if missing:
        names = {agent.name for agent in missing}
        ids = {_profile_user_id(agent) for agent in missing}
        found = await session.execute(
            select(OAuthAccess.user_id, OAuthClient.name, OAuthClient.id)
            .join(OAuthClient, OAuthClient.id == OAuthAccess.client_id)
            .where(OAuthAccess.user_id.in_(ids), OAuthClient.name.in_(names))
            .order_by(OAuthAccess.created_at.desc())
        )
        for user_id, name, client_id in found.all():
            clients.setdefault((user_id, name), client_id)
    out: dict[str, dict[str, str]] = {}
    for agent in agents:
        user_id = _profile_user_id(agent)
        user = users.get(user_id)
        if user is not None and user.organization_id != agent.organization_id:
            user = None
        client_id = str((agent.settings or {}).get("client_id") or "").strip()
        if not client_id and user is not None:
            client_id = clients.get((user.id, agent.name), "")
        out[agent.id] = {
            "user_id": user.id if user is not None else "",
            "name": user.name if user is not None else "",
            "email": user.email if user is not None else "",
            "client_id": client_id,
        }
    return out


def serialize_agent(agent, pushed: bool | None = None, registration: dict[str, str] | None = None) -> AgentRead:
    reg = registration or {}
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
        client_id=reg.get("client_id") or None,
        registered_user_id=reg.get("user_id") or None,
        registered_user_name=reg.get("name") or None,
        registered_user_email=reg.get("email") or None,
        pushed=pushed,
    )


@router.get("", response_model=list[AgentRead])
async def list_agents(session: SessionDep, user: CurrentUser, _access: AgentsRead):
    await mapping_service.bind_workspace(session, user)
    agents = await agent_service.list_agents(session, user)
    registrations = await _registrations(session, agents)
    return [serialize_agent(agent, registration=registrations.get(agent.id)) for agent in agents]


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
    registrations = await _registrations(session, [agent])
    return serialize_agent(agent, registration=registrations.get(agent.id))


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
    registrations = await _registrations(session, [agent])
    return serialize_agent(agent, registration=registrations.get(agent.id))


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
    registrations = await _registrations(session, [agent])
    return serialize_agent(agent, pushed=False, registration=registrations.get(agent.id))


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, session: SessionDep, user: CurrentUser, _access: TeamWrite):
    await removal_service.delete_agent(session, user, agent_id)

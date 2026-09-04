from __future__ import annotations

import secrets

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ids import new_id, utcnow
from app.models import Agent, Connector, McpServer, Tool
from app.models.enums import AgentStatus, ToolStatus, ToolType
from app.schemas import ConnectorEnrollResponse, ConnectorRead
from app.services.agent_config import default_instructions
from app.services.agent_hub import agent_hub
from app.services.auth_service import hash_token
from app.services.mapping_service import mapping_service
from app.services.wallet_service import wallet_service


def enrollment_payload(connector_id: str, token: str) -> ConnectorEnrollResponse:
    return ConnectorEnrollResponse(
        connector_id=connector_id,
        token=token,
        ws_url=f"/ws/connectors?connector_id={connector_id}&token={token}",
    )


def _slug_id(prefix: str, name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return f"{prefix}_{slug}" if slug else new_id(prefix)


def _display_name(ident: str, prefix: str) -> str:
    slug = ident
    if slug.startswith(f"{prefix}_"):
        slug = slug[len(prefix) + 1 :]
    label = slug.replace("_", " ").replace("-", " ").strip()
    return label.title() if label else ident


def serialize_connector(connector: Connector | None, organization_id: str) -> ConnectorRead:
    if connector is None:
        return ConnectorRead(
            enrolled=False,
            connected=False,
            organization_id=organization_id,
        )
    live = agent_hub.connector_snapshot(organization_id)
    return ConnectorRead(
        id=connector.id,
        name=connector.name,
        enrolled=bool(connector.enrollment_token or connector.token_hash),
        connected=bool(live and live.get("connected")),
        environment_id=connector.environment_id,
        version=connector.version,
        capabilities=[str(item) for item in (connector.capabilities or [])],
        last_seen_at=connector.last_seen_at,
        organization_id=connector.organization_id,
    )


class ConnectorService:
    async def get_for_org(
        self, session: AsyncSession, organization_id: str
    ) -> Connector | None:
        result = await session.execute(
            select(Connector).where(Connector.organization_id == organization_id)
        )
        return result.scalar_one_or_none()

    async def get_by_token(self, session: AsyncSession, token: str) -> Connector | None:
        if not token:
            return None
        digest = hash_token(token)
        result = await session.execute(
            select(Connector).where(Connector.token_hash == digest)
        )
        return result.scalar_one_or_none()

    async def ensure(
        self, session: AsyncSession, organization_id: str, *, connector_id: str | None = None
    ) -> Connector:
        existing = await self.get_for_org(session, organization_id)
        if existing is not None:
            return existing
        ident = connector_id or new_id("conn")
        if await session.get(Connector, ident) is not None:
            ident = new_id("conn")
        connector = Connector(
            id=ident,
            organization_id=organization_id,
            name="Saturn Connector",
            capabilities=["agent_runtime", "mcp_gateway"],
        )
        session.add(connector)
        await session.flush()
        return connector

    async def issue_token(
        self, session: AsyncSession, organization_id: str
    ) -> ConnectorEnrollResponse:
        connector = await self.ensure(session, organization_id)
        raw = "conn_" + secrets.token_urlsafe(32)
        connector.token_hash = hash_token(raw)
        connector.enrollment_token = raw
        await session.flush()
        return enrollment_payload(connector.id, raw)

    async def get_enrollment(
        self, session: AsyncSession, organization_id: str
    ) -> ConnectorEnrollResponse:
        connector = await self.get_for_org(session, organization_id)
        if connector is None or not connector.enrollment_token:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not enrolled")
        return enrollment_payload(connector.id, connector.enrollment_token)

    async def touch(
        self,
        session: AsyncSession,
        connector: Connector,
        *,
        environment_id: str = "",
        version: str = "",
        capabilities: list[str] | None = None,
        remote_id: str = "",
    ) -> Connector:
        connector.last_seen_at = utcnow()
        if environment_id:
            connector.environment_id = environment_id
        if version:
            connector.version = version
        if capabilities:
            connector.capabilities = capabilities
        if remote_id and remote_id != connector.id:
            connector.name = connector.name or remote_id
        await session.flush()
        return connector

    async def upsert_runtime_agents(
        self,
        session: AsyncSession,
        connector: Connector,
        *,
        runtime_id: str,
        agents: list[str],
        name: str = "",
        role: str = "",
    ) -> list[Agent]:
        created: list[Agent] = []
        for raw in agents:
            ident = str(raw).strip()
            if not ident:
                continue
            wanted_id = ident if ident.startswith("agent_") else _slug_id("agent", ident)
            agent = await session.get(Agent, wanted_id) or await session.get(Agent, ident)
            if agent is None:
                label = _display_name(ident, "agent") or name or ident
                agent = Agent(
                    id=wanted_id,
                    organization_id=connector.organization_id,
                    name=label,
                    description=role,
                    model="claude-sonnet",
                    status=AgentStatus.ACTIVE,
                    connector_id=connector.id,
                    runtime_id=runtime_id or None,
                    settings={"instructions": default_instructions(label, role)},
                )
                session.add(agent)
                await session.flush()
                await wallet_service.ensure_wallet(session, agent)
            elif agent.organization_id != connector.organization_id:
                continue
            else:
                agent.connector_id = connector.id
                if runtime_id:
                    agent.runtime_id = runtime_id
                agent.last_seen_at = utcnow()
                if name and not agent.description:
                    agent.description = name
            created.append(agent)
        if created:
            result = await session.execute(
                select(Agent)
                .options(selectinload(Agent.mcp_servers), selectinload(Agent.employees))
                .where(Agent.id.in_([agent.id for agent in created]))
            )
            created = list(result.scalars().unique().all())
        await mapping_service.share_org_agents(session, connector.organization_id, created)
        await session.flush()
        return created

    async def upsert_mcp(
        self,
        session: AsyncSession,
        connector: Connector,
        *,
        server_id: str,
        tools: list[str],
        url: str = "",
        name: str = "",
    ) -> McpServer:
        ident = server_id.strip() or new_id("mcp")
        server = await session.get(McpServer, ident)
        if server is None:
            result = await session.execute(
                select(McpServer).where(
                    McpServer.organization_id == connector.organization_id,
                    McpServer.name == (name or ident),
                )
            )
            server = result.scalar_one_or_none()
        if server is None:
            server = McpServer(
                id=ident if not ident.startswith("http") else new_id("mcp"),
                organization_id=connector.organization_id,
                name=name or _display_name(ident, "mcp"),
                endpoint=url,
                status="active",
                connector_id=connector.id,
            )
            session.add(server)
            await session.flush()
        else:
            if server.organization_id != connector.organization_id:
                raise HTTPException(status.HTTP_409_CONFLICT, "MCP server exists")
            server.connector_id = connector.id
            if url:
                server.endpoint = url
            if name:
                server.name = name
            server.status = "active"

        result = await session.execute(
            select(Tool).where(Tool.mcp_server_id == server.id)
        )
        existing = {tool.name: tool for tool in result.scalars().all()}
        result = await session.execute(
            select(Tool).where(Tool.organization_id == connector.organization_id)
        )
        by_name = {tool.name: tool for tool in result.scalars().all()}
        for tool_name in tools:
            label = str(tool_name).strip()
            if not label:
                continue
            tool = existing.get(label) or by_name.get(label)
            if tool is None:
                tool = Tool(
                    id=_slug_id("tool", label),
                    organization_id=connector.organization_id,
                    name=label,
                    description=label,
                    type=ToolType.MCP.value,
                    mcp_endpoint=url or server.endpoint,
                    permissions=["read"],
                    mcp_server_id=server.id,
                    status=ToolStatus.ACTIVE.value,
                )
                if await session.get(Tool, tool.id) is not None:
                    tool.id = new_id("tool")
                session.add(tool)
            else:
                tool.mcp_server_id = server.id
                if url:
                    tool.mcp_endpoint = url
                tool.status = ToolStatus.ACTIVE.value
        await session.flush()
        result = await session.execute(
            select(McpServer)
            .options(selectinload(McpServer.tools), selectinload(McpServer.agents))
            .where(McpServer.id == server.id)
        )
        server = result.scalar_one()
        await mapping_service.share_org_mcp(session, connector.organization_id, [server])
        await session.flush()
        return server


connector_service = ConnectorService()

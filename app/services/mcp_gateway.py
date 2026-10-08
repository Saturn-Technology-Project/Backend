from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import McpServer


@dataclass
class GatewayLink:
    server_id: str
    organization_id: str
    name: str
    endpoint: str
    connected: bool
    connected_at: datetime | None = None
    error: str | None = None


class McpGateway:
    """In-process MCP gateway. Login binds every MCP server that belongs to the org."""

    def __init__(self) -> None:
        self._links: dict[str, GatewayLink] = {}

    async def connect_organization(
        self, session: AsyncSession, organization_id: str
    ) -> list[GatewayLink]:
        result = await session.execute(
            select(McpServer)
            .options(selectinload(McpServer.tools))
            .where(McpServer.organization_id == organization_id)
            .order_by(McpServer.name.asc())
        )
        servers = list(result.scalars().unique().all())
        links: list[GatewayLink] = []
        for server in servers:
            linked = self._connect_server(server)
            links.append(linked)
        return links

    def _connect_server(self, server: McpServer) -> GatewayLink:
        existing = self._links.get(server.id)
        if existing is not None and existing.endpoint == server.endpoint and not existing.connected:
            return existing
        link = GatewayLink(
            server_id=server.id,
            organization_id=server.organization_id,
            name=server.name,
            endpoint=server.endpoint,
            connected=False,
            connected_at=None,
            error=None,
        )
        self._links[server.id] = link
        return link

    def reset(self) -> None:
        self._links.clear()

    def link_for(self, server_id: str) -> GatewayLink | None:
        return self._links.get(server_id)

    def is_connected(self, server_id: str) -> bool:
        link = self._links.get(server_id)
        return bool(link and link.connected)

    def snapshot(self, organization_id: str) -> dict:
        links = [
            link
            for link in self._links.values()
            if link.organization_id == organization_id
        ]
        connected = sum(1 for link in links if link.connected)
        return {
            "status": "connected" if links and connected == len(links) else (
                "degraded" if connected else "disconnected"
            ),
            "connected": connected,
            "total": len(links),
            "servers": [
                {
                    "id": link.server_id,
                    "name": link.name,
                    "endpoint": link.endpoint,
                    "connected": link.connected,
                    "error": link.error,
                }
                for link in sorted(links, key=lambda item: item.name)
            ],
        }


mcp_gateway = McpGateway()

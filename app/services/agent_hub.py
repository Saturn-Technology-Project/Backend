from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.core.ids import new_id, utcnow


def _json_safe(message: dict[str, Any]) -> dict[str, Any]:
    return message


@dataclass
class AgentLink:
    agent_id: str
    organization_id: str
    websocket: WebSocket
    runtime_id: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_seen_at: Any = field(default_factory=utcnow)
    last_heartbeat_at: Any = None


@dataclass
class PanelLink:
    user_id: str
    organization_id: str
    websocket: WebSocket
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class ConnectorLink:
    connector_id: str
    organization_id: str
    websocket: WebSocket
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_seen_at: Any = field(default_factory=utcnow)
    last_heartbeat_at: Any = None
    agent_ids: set[str] = field(default_factory=set)


class AgentHub:
    """In-memory control plane: connectors/agents dial in, the panel issues commands."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentLink] = {}
        self._connectors: dict[str, ConnectorLink] = {}
        self._panels: dict[str, list[PanelLink]] = {}
        self._pending: dict[str, dict[str, dict[str, Any]]] = {}

    def is_connected(self, agent_id: str) -> bool:
        link = self._agents.get(agent_id)
        return link is not None and link.websocket.client_state == WebSocketState.CONNECTED

    def last_seen(self, agent_id: str):
        link = self._agents.get(agent_id)
        return None if link is None else link.last_seen_at

    def connected_ids(self, organization_id: str) -> list[str]:
        return [
            link.agent_id
            for link in self._agents.values()
            if link.organization_id == organization_id and self.is_connected(link.agent_id)
        ]

    def last_heartbeat(self, agent_id: str):
        link = self._agents.get(agent_id)
        if link is None:
            return None
        return link.last_heartbeat_at or link.last_seen_at

    def runtime_id(self, agent_id: str) -> str:
        link = self._agents.get(agent_id)
        return "" if link is None else (link.runtime_id or "")

    def connector_snapshot(self, organization_id: str) -> dict[str, Any] | None:
        link = self._connectors.get(organization_id)
        if link is None:
            return None
        heartbeat = link.last_heartbeat_at or link.last_seen_at
        return {
            "id": link.connector_id,
            "connected": link.websocket.client_state == WebSocketState.CONNECTED,
            "last_heartbeat_at": heartbeat.isoformat() if heartbeat is not None else None,
            "agent_ids": sorted(link.agent_ids),
        }

    def snapshot(self, organization_id: str) -> dict[str, Any]:
        ids = self.connected_ids(organization_id)
        connector = self.connector_snapshot(organization_id)
        return {
            "type": "snapshot",
            "connector": connector,
            "agents": [
                {
                    "id": agent_id,
                    "connected": True,
                    "runtime_id": self._agents[agent_id].runtime_id or None,
                    "last_seen_at": (
                        self._agents[agent_id].last_seen_at.isoformat()
                        if self._agents[agent_id].last_seen_at is not None
                        else None
                    ),
                    "last_heartbeat_at": (
                        self.last_heartbeat(agent_id).isoformat()
                        if self.last_heartbeat(agent_id) is not None
                        else None
                    ),
                }
                for agent_id in ids
            ],
        }

    async def register_connector(
        self, *, connector_id: str, organization_id: str, websocket: WebSocket
    ) -> ConnectorLink:
        existing = self._connectors.get(organization_id)
        if existing is not None and existing.websocket is not websocket:
            await _close(existing.websocket, 4000, "replaced")
            await self.unregister_websocket(existing.websocket)
        link = ConnectorLink(
            connector_id=connector_id,
            organization_id=organization_id,
            websocket=websocket,
        )
        self._connectors[organization_id] = link
        await self.broadcast_org(
            organization_id,
            {"type": "connector.online", "connector_id": connector_id},
        )
        return link

    async def unregister_connector(self, organization_id: str, websocket: WebSocket) -> None:
        link = self._connectors.get(organization_id)
        if link is None or link.websocket is not websocket:
            return
        self._connectors.pop(organization_id, None)
        await self.unregister_websocket(websocket)
        await self.broadcast_org(
            organization_id,
            {"type": "connector.offline", "connector_id": link.connector_id},
        )

    def touch_connector(self, organization_id: str, *, heartbeat: bool = False) -> None:
        link = self._connectors.get(organization_id)
        if link is None:
            return
        now = utcnow()
        link.last_seen_at = now
        if heartbeat:
            link.last_heartbeat_at = now

    async def attach_connector_agents(
        self,
        *,
        organization_id: str,
        websocket: WebSocket,
        agent_ids: list[str],
        runtime_id: str = "",
    ) -> None:
        link = self._connectors.get(organization_id)
        for agent_id in agent_ids:
            await self.register_agent(
                agent_id=agent_id,
                organization_id=organization_id,
                websocket=websocket,
            )
            self.touch(agent_id, runtime_id=runtime_id)
            if link is not None:
                link.agent_ids.add(agent_id)

    async def unregister_websocket(self, websocket: WebSocket) -> None:
        agent_ids = [
            agent_id
            for agent_id, link in list(self._agents.items())
            if link.websocket is websocket
        ]
        for agent_id in agent_ids:
            await self.unregister_agent(agent_id, websocket)

    async def register_agent(
        self, *, agent_id: str, organization_id: str, websocket: WebSocket
    ) -> None:
        existing = self._agents.get(agent_id)
        if existing is not None and existing.websocket is not websocket:
            await _close(existing.websocket, 4000, "replaced")
        self._agents[agent_id] = AgentLink(
            agent_id=agent_id,
            organization_id=organization_id,
            websocket=websocket,
        )
        await self.broadcast_org(
            organization_id,
            {"type": "agent.online", "agent_id": agent_id},
        )

    async def unregister_agent(self, agent_id: str, websocket: WebSocket) -> None:
        link = self._agents.get(agent_id)
        if link is None or link.websocket is not websocket:
            return
        org_id = link.organization_id
        self._agents.pop(agent_id, None)
        await self.broadcast_org(
            org_id,
            {"type": "agent.offline", "agent_id": agent_id},
        )

    def touch(self, agent_id: str, *, heartbeat: bool = False, runtime_id: str = "") -> None:
        link = self._agents.get(agent_id)
        if link is None:
            return
        now = utcnow()
        link.last_seen_at = now
        if heartbeat:
            link.last_heartbeat_at = now
        if runtime_id:
            link.runtime_id = runtime_id

    def command_envelope(
        self,
        command: str,
        payload: dict[str, Any],
        command_id: str | None = None,
        *,
        agent_id: str = "",
        runtime_id: str = "",
    ) -> dict[str, Any]:
        cmd_id = command_id or new_id("cmd")
        body = dict(payload)
        body.setdefault("command_id", cmd_id)
        if agent_id:
            body.setdefault("agent_id", agent_id)
        if runtime_id:
            body.setdefault("runtime_id", runtime_id)
        message = {
            "type": "command",
            "command": command,
            "command_id": cmd_id,
            "id": cmd_id,
            "payload": body,
        }
        if agent_id:
            message["agent_id"] = agent_id
        if runtime_id:
            message["runtime_id"] = runtime_id
        return message

    def track_command(self, agent_id: str, message: dict[str, Any]) -> None:
        command_id = str(message.get("command_id") or message.get("id") or "")
        if not command_id:
            return
        bucket = self._pending.setdefault(agent_id, {})
        if message.get("command") == "config.update":
            for cid, item in list(bucket.items()):
                if item.get("command") == "config.update":
                    bucket.pop(cid, None)
        bucket[command_id] = message

    def complete_command(self, agent_id: str, command_id: str | None) -> None:
        if not command_id:
            return
        bucket = self._pending.get(agent_id)
        if bucket is not None:
            bucket.pop(str(command_id), None)

    async def send_command(
        self, agent_id: str, command: str, payload: dict[str, Any]
    ) -> bool:
        link = self._agents.get(agent_id)
        runtime_id = link.runtime_id if link else ""
        message = self.command_envelope(
            command, payload, agent_id=agent_id, runtime_id=runtime_id
        )
        if command != "ping":
            self.track_command(agent_id, message)
        delivered = await self.send_agent(agent_id, message)
        if delivered and command == "config.update":
            self.complete_command(agent_id, message.get("command_id"))
        return delivered

    async def replay_pending(self, agent_id: str) -> int:
        pending = list((self._pending.get(agent_id) or {}).values())
        sent = 0
        for message in pending:
            if await self.send_agent(agent_id, message):
                sent += 1
        return sent

    async def register_panel(
        self, *, user_id: str, organization_id: str, websocket: WebSocket
    ) -> PanelLink:
        link = PanelLink(
            user_id=user_id,
            organization_id=organization_id,
            websocket=websocket,
        )
        self._panels.setdefault(organization_id, []).append(link)
        await _send(link.websocket, link.lock, self.snapshot(organization_id))
        return link

    async def unregister_panel(self, link: PanelLink) -> None:
        group = self._panels.get(link.organization_id) or []
        self._panels[link.organization_id] = [item for item in group if item is not link]

    async def send_agent(self, agent_id: str, message: dict[str, Any]) -> bool:
        link = self._agents.get(agent_id)
        if link is None or not self.is_connected(agent_id):
            return False
        return await _send(link.websocket, link.lock, message)

    def reset(self) -> None:
        self._agents.clear()
        self._connectors.clear()
        self._panels.clear()
        self._pending.clear()

    async def send_panel(self, websocket: WebSocket, message: dict[str, Any]) -> bool:
        for group in self._panels.values():
            for link in group:
                if link.websocket is websocket:
                    return await _send(link.websocket, link.lock, message)
        return await _send(websocket, asyncio.Lock(), message)

    async def broadcast_org(self, organization_id: str, message: dict[str, Any]) -> None:
        for link in list(self._panels.get(organization_id) or []):
            await _send(link.websocket, link.lock, message)


async def _send(websocket: WebSocket, lock: asyncio.Lock, message: dict[str, Any]) -> bool:
    if websocket.client_state != WebSocketState.CONNECTED:
        return False
    try:
        async with lock:
            await websocket.send_json(_json_safe(message))
        return True
    except Exception:
        return False


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        return


agent_hub = AgentHub()

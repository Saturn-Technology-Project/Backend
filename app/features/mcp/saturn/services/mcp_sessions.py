from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

LIST_CHANGED = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}


@dataclass
class McpSession:
    session_id: str
    user_id: str
    agent_id: str
    client_id: str
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)


class McpSessionHub:
    def __init__(self) -> None:
        self._items: dict[str, McpSession] = {}

    def open(self, session_id: str, user_id: str, agent_id: str, client_id: str) -> McpSession:
        current = self._items.get(session_id)
        if current is not None and current.user_id == user_id and current.client_id == client_id:
            current.agent_id = agent_id
            return current
        for sid, item in list(self._items.items()):
            if sid != session_id and item.agent_id == agent_id and item.client_id == client_id:
                self.close(sid)
        record = McpSession(
            session_id=session_id,
            user_id=user_id,
            agent_id=agent_id,
            client_id=client_id,
        )
        self._items[session_id] = record
        return record

    def get(self, session_id: str, user_id: str, client_id: str) -> McpSession | None:
        record = self._items.get(session_id)
        if record is None or record.user_id != user_id or record.client_id != client_id:
            return None
        return record

    def notify(self, agent_ids: list[str]) -> None:
        wanted = {agent_id for agent_id in agent_ids if agent_id}
        if not wanted:
            return
        for record in self._items.values():
            if record.agent_id in wanted:
                record.queue.put_nowait(dict(LIST_CHANGED))

    def close(self, session_id: str) -> None:
        record = self._items.pop(session_id, None)
        if record is not None:
            record.queue.put_nowait(None)

    def reset(self) -> None:
        for session_id in list(self._items):
            self.close(session_id)


mcp_sessions = McpSessionHub()

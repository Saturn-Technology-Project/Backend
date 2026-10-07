from __future__ import annotations

from app.core.ids import utcnow
from app.models.user_app_mcp import UserAppMcp
from sqlalchemy.ext.asyncio import AsyncSession

_MAX_SERVERS = 40
_MAX_TOOLS = 80
_MAX_AGENTS = 80


def _text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _http_url(value: object) -> str:
    text = _text(value, 512)
    if text.startswith("https://") or text.startswith("http://"):
        return text
    return ""


def _owned(server_id: str) -> bool:
    return bool(server_id) and not server_id.startswith("saturn:") and server_id != "general-skills"


class AppMcpService:
    async def read(self, session: AsyncSession, user_id: str) -> dict:
        row = await session.get(UserAppMcp, user_id)
        if row is None:
            return {"servers": [], "attachments": {}}
        return {"servers": list(row.servers or []), "attachments": dict(row.attachments or {})}

    async def replace(self, session: AsyncSession, user_id: str, body: dict) -> dict:
        cleaned = self._clean(body)
        row = await session.get(UserAppMcp, user_id)
        if row is None:
            row = UserAppMcp(user_id=user_id, servers=[], attachments={})
            session.add(row)
        row.servers = cleaned["servers"]
        row.attachments = cleaned["attachments"]
        row.updated_at = utcnow()
        await session.flush()
        return cleaned

    def _clean(self, body: dict) -> dict:
        raw_servers = body.get("servers") if isinstance(body.get("servers"), list) else []
        servers: list[dict] = []
        ids: set[str] = set()
        for raw in raw_servers[:_MAX_SERVERS]:
            if not isinstance(raw, dict):
                continue
            ident = _text(raw.get("id"), 64)
            name = _text(raw.get("name"), 255)
            url = _http_url(raw.get("url"))
            if not _owned(ident) or not name or not url or ident in ids:
                continue
            tools: list[dict] = []
            seen: set[str] = set()
            raw_tools = raw.get("tools") if isinstance(raw.get("tools"), list) else []
            for tool in raw_tools[:_MAX_TOOLS]:
                if not isinstance(tool, dict):
                    continue
                tool_id = _text(tool.get("tool_id") or tool.get("toolId"), 128)
                if not tool_id or tool_id in seen:
                    continue
                seen.add(tool_id)
                tools.append({"tool_id": tool_id, "description": _text(tool.get("description"), 500)})
            ids.add(ident)
            servers.append({"id": ident, "name": name, "url": url, "tools": tools})

        raw_attachments = body.get("attachments") if isinstance(body.get("attachments"), dict) else {}
        attachments: dict[str, list[str]] = {}
        for agent_id, chosen in list(raw_attachments.items())[:_MAX_AGENTS]:
            agent = _text(agent_id, 64)
            if not agent or agent == "saturn-agent" or not isinstance(chosen, list):
                continue
            kept = []
            seen_ids: set[str] = set()
            for item in chosen:
                server_id = _text(item, 64)
                if server_id in ids and server_id not in seen_ids:
                    seen_ids.add(server_id)
                    kept.append(server_id)
            if kept:
                attachments[agent] = kept
        return {"servers": servers, "attachments": attachments}


app_mcp_service = AppMcpService()

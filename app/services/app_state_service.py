from __future__ import annotations

from app.core.ids import utcnow
from app.models.user_app_state import UserAppState
from sqlalchemy.ext.asyncio import AsyncSession

_MAX_AGENTS = 80
_MAX_MESSAGES = 200
_MAX_ACTIONS = 20


def _text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def clean_agents(raw_agents: object, scope: str) -> list[dict]:
    agents: list[dict] = []
    ids: set[str] = set()
    rows = raw_agents if isinstance(raw_agents, list) else []
    for raw in rows[:_MAX_AGENTS]:
        if not isinstance(raw, dict):
            continue
        ident = _text(raw.get("id"), 64)
        name = _text(raw.get("name"), 255)
        if not ident or ident == "saturn-agent" or not name or ident in ids:
            continue
        ids.add(ident)
        mcp_ids = raw.get("mcp_connection_ids") if isinstance(raw.get("mcp_connection_ids"), list) else []
        rag_ids = raw.get("rag_connection_ids") if isinstance(raw.get("rag_connection_ids"), list) else []
        if _text(raw.get("scope"), 16) != scope:
            continue
        agents.append(
            {
                "id": ident,
                "name": name,
                "description": _text(raw.get("description"), 2000),
                "instructions": _text(raw.get("instructions"), 8000),
                "response_style": _text(raw.get("response_style"), 64) or "professional",
                "autonomy": _text(raw.get("autonomy"), 64) or "execute_when_allowed",
                "temperature": _number(raw.get("temperature"), 0.2, 0, 2),
                "max_output": int(_number(raw.get("max_output"), 4000, 1, 128000)),
                "scope": scope,
                "model_connection_id": _text(raw.get("model_connection_id"), 64),
                "mcp_connection_ids": [_text(item, 64) for item in mcp_ids if _text(item, 64) and _text(item, 64) != "general-skills"][:40],
                "rag_connection_ids": [_text(item, 64) for item in rag_ids if _text(item, 64)][:40],
                "has_saturn_mcp": raw.get("has_saturn_mcp") is True,
            }
        )
    return agents


def _number(value: object, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return min(high, max(low, number))


class AppStateService:
    async def read(self, session: AsyncSession, user_id: str) -> dict:
        row = await session.get(UserAppState, user_id)
        if row is None:
            return {"agents": [], "messages": []}
        return {"agents": list(row.agents or []), "messages": list(row.messages or [])}

    async def replace(self, session: AsyncSession, user_id: str, body: dict, team_ids: set[str] | None = None) -> dict:
        current = await self.read(session, user_id)
        cleaned = self._clean(body, current, team_ids or set())
        row = await session.get(UserAppState, user_id)
        if row is None:
            row = UserAppState(user_id=user_id, agents=[], messages=[])
            session.add(row)
        row.agents = cleaned["agents"]
        row.messages = cleaned["messages"]
        row.updated_at = utcnow()
        await session.flush()
        return cleaned

    def _clean(self, body: dict, current: dict, team_ids: set[str]) -> dict:
        agents = clean_agents(body.get("agents"), "personal")
        ids = {item["id"] for item in agents}
        kept = ids | team_ids

        incoming = body.get("messages") if isinstance(body.get("messages"), list) else []
        by_id: dict[str, dict] = {}
        for raw in list(current.get("messages") or []):
            if isinstance(raw, dict) and _text(raw.get("id"), 64):
                by_id[_text(raw.get("id"), 64)] = raw
        for raw in incoming:
            message = self._message(raw, kept)
            if message is not None:
                previous = by_id.get(message["id"])
                if previous is None or str(message.get("updated_at") or "") >= str(previous.get("updated_at") or ""):
                    by_id[message["id"]] = message
        messages = [item for item in by_id.values() if item.get("agent_id") in kept]
        messages.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return {"agents": agents, "messages": messages[:_MAX_MESSAGES]}

    def _message(self, raw: object, agent_ids: set[str]) -> dict | None:
        if not isinstance(raw, dict):
            return None
        ident = _text(raw.get("id"), 64)
        agent_id = _text(raw.get("agent_id"), 64)
        text = _text(raw.get("input"), 8000)
        if not ident or not text or agent_id not in agent_ids:
            return None
        actions = []
        raw_actions = raw.get("actions") if isinstance(raw.get("actions"), list) else []
        for action in raw_actions[:_MAX_ACTIONS]:
            if not isinstance(action, dict):
                continue
            actions.append(
                {
                    "toolId": _text(action.get("toolId") or action.get("tool_id"), 128),
                    "status": _text(action.get("status"), 32) or "completed",
                }
            )
        status = _text(raw.get("status"), 32) or "completed"
        return {
            "id": ident,
            "agent_id": agent_id,
            "input": text,
            "output": _text(raw.get("output"), 8000),
            "status": status,
            "actions": actions,
            "error": _text(raw.get("error"), 2000),
            "created_at": _text(raw.get("created_at"), 40),
            "updated_at": _text(raw.get("updated_at"), 40) or _text(raw.get("created_at"), 40),
        }


app_state_service = AppStateService()

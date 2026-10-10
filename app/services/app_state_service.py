from __future__ import annotations

from app.core.ids import utcnow
from app.models.user_app_state import UserAppState
from sqlalchemy.ext.asyncio import AsyncSession

_MAX_AGENTS = 80
_MAX_MESSAGES = 200
_MAX_ACTIONS = 20
_MAX_TASKS = 40
_TASK_STATUSES = {"working", "input_required", "completed", "failed", "cancelled"}
_STEP_STATUSES = {"pending", "working", "completed", "failed"}


def _text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _clock(value: str) -> bool:
    if len(value) != 5 or value[2] != ":":
        return False
    hours, minutes = value.split(":", 1)
    return hours.isdigit() and minutes.isdigit() and int(hours) < 24 and int(minutes) < 60


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
                "runtime_base": _runtime_base(raw.get("runtime_base")) if scope == "team" else "",
            }
        )
    return agents


def _runtime_base(value: object) -> str:
    text = _text(value, 200)
    if not text:
        return ""
    from urllib.parse import urlsplit

    parts = urlsplit(text if "://" in text else f"http://{text}")
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


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
            return {"agents": [], "messages": [], "tasks": [], "wakes": [], "memories": []}
        return {
            "agents": list(row.agents or []),
            "messages": list(row.messages or []),
            "tasks": list(row.tasks or []),
            "wakes": list(row.wakes or []),
            "memories": list(row.memories or []),
        }

    async def replace(self, session: AsyncSession, user_id: str, body: dict, team_ids: set[str] | None = None) -> dict:
        current = await self.read(session, user_id)
        cleaned = self._clean(body, current, team_ids or set())
        row = await session.get(UserAppState, user_id)
        if row is None:
            row = UserAppState(user_id=user_id, agents=[], messages=[], tasks=[], wakes=[], memories=[])
            session.add(row)
        row.agents = cleaned["agents"]
        row.messages = cleaned["messages"]
        row.tasks = cleaned["tasks"]
        row.wakes = cleaned["wakes"]
        row.memories = cleaned["memories"]
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
        tasks = list(current.get("tasks") or []) if body.get("tasks") is None else self._tasks(body.get("tasks"), current, kept)
        wakes = list(current.get("wakes") or []) if body.get("wakes") is None else self._wakes(body.get("wakes"), current, kept)
        memories = list(current.get("memories") or []) if body.get("memories") is None else self._memories(body.get("memories"), current, kept)
        return {"agents": agents, "messages": messages[:_MAX_MESSAGES], "tasks": tasks, "wakes": wakes, "memories": memories}

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

    def _tasks(self, raw_tasks: object, current: dict, agent_ids: set[str]) -> list[dict]:
        by_id: dict[str, dict] = {}
        for raw in list(current.get("tasks") or []):
            if isinstance(raw, dict) and _text(raw.get("id"), 64):
                by_id[_text(raw.get("id"), 64)] = raw
        rows = raw_tasks if isinstance(raw_tasks, list) else []
        for raw in rows:
            task = self._task(raw, agent_ids)
            if task is None:
                continue
            previous = by_id.get(task["id"])
            if previous is None or str(task.get("updated_at") or "") >= str(previous.get("updated_at") or ""):
                by_id[task["id"]] = task
        tasks = [item for item in by_id.values() if item.get("agent_id") in agent_ids]
        tasks.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return tasks[:_MAX_TASKS]

    def _task(self, raw: object, agent_ids: set[str]) -> dict | None:
        if not isinstance(raw, dict):
            return None
        ident = _text(raw.get("id"), 64)
        agent_id = _text(raw.get("agent_id"), 64)
        goal = _text(raw.get("goal"), 4000)
        status = _text(raw.get("status"), 32)
        if not ident or not goal or agent_id not in agent_ids or status not in _TASK_STATUSES:
            return None
        steps = []
        for step in (raw.get("steps") if isinstance(raw.get("steps"), list) else [])[:24]:
            if not isinstance(step, dict):
                continue
            step_status = _text(step.get("status"), 32)
            title = _text(step.get("title"), 240)
            step_id = _text(step.get("id"), 80)
            if not step_id or not title or step_status not in _STEP_STATUSES:
                continue
            steps.append({"id": step_id, "title": title, "status": step_status})
        if not steps:
            return None
        checkpoint = raw.get("checkpoint") if isinstance(raw.get("checkpoint"), dict) else None
        artifacts = []
        for artifact in (raw.get("artifacts") if isinstance(raw.get("artifacts"), list) else [])[:8]:
            if not isinstance(artifact, dict):
                continue
            artifact_id = _text(artifact.get("id"), 80)
            title = _text(artifact.get("title"), 240)
            if not artifact_id or not title:
                continue
            artifacts.append({"id": artifact_id, "title": title, "body": _text(artifact.get("body"), 8000)})
        run_ids = [_text(item, 64) for item in (raw.get("run_ids") if isinstance(raw.get("run_ids"), list) else []) if _text(item, 64)][:40]
        return {
            "id": ident,
            "agent_id": agent_id,
            "goal": goal,
            "status": status,
            "steps": steps,
            "checkpoint": None
            if checkpoint is None
            else {
                "at": _text(checkpoint.get("at"), 40),
                "summary": _text(checkpoint.get("summary"), 2000),
                "step_id": _text(checkpoint.get("step_id"), 80),
            },
            "artifacts": artifacts,
            "run_ids": run_ids,
            "created_at": _text(raw.get("created_at"), 40),
            "updated_at": _text(raw.get("updated_at"), 40) or _text(raw.get("created_at"), 40),
            **({"wake_id": wake_id} if (wake_id := _text(raw.get("wake_id"), 64)) else {}),
        }

    def _wakes(self, raw_wakes: object, current: dict, agent_ids: set[str]) -> list[dict]:
        by_id: dict[str, dict] = {}
        for raw in list(current.get("wakes") or []):
            if isinstance(raw, dict) and _text(raw.get("id"), 64):
                by_id[_text(raw.get("id"), 64)] = raw
        rows = raw_wakes if isinstance(raw_wakes, list) else []
        for raw in rows:
            wake = self._wake(raw, agent_ids)
            if wake is None:
                continue
            previous = by_id.get(wake["id"])
            if previous is None or str(wake.get("updated_at") or "") >= str(previous.get("updated_at") or ""):
                by_id[wake["id"]] = wake
        wakes = [item for item in by_id.values() if item.get("agent_id") in agent_ids]
        wakes.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return wakes[:20]

    def _memories(self, raw_memories: object, current: dict, agent_ids: set[str]) -> list[dict]:
        by_id: dict[str, dict] = {}
        for raw in list(current.get("memories") or []):
            if isinstance(raw, dict) and _text(raw.get("id"), 64):
                by_id[_text(raw.get("id"), 64)] = raw
        rows = raw_memories if isinstance(raw_memories, list) else []
        for raw in rows:
            note = self._memory(raw, agent_ids)
            if note is not None:
                by_id[note["id"]] = note
        notes = [item for item in by_id.values() if item.get("agent_id") in agent_ids]
        notes.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        kept: list[dict] = []
        counts: dict[str, int] = {}
        for item in notes:
            agent_id = str(item.get("agent_id") or "")
            counts[agent_id] = counts.get(agent_id, 0) + 1
            if counts[agent_id] <= 40:
                kept.append(item)
        return kept[:80]

    def _memory(self, raw: object, agent_ids: set[str]) -> dict | None:
        if not isinstance(raw, dict):
            return None
        ident = _text(raw.get("id"), 64)
        agent_id = _text(raw.get("agent_id"), 64)
        body = _text(raw.get("body"), 500)
        if not ident or not body or agent_id not in agent_ids:
            return None
        return {
            "id": ident,
            "agent_id": agent_id,
            "body": body,
            "created_at": _text(raw.get("created_at"), 40),
        }

    def _wake(self, raw: object, agent_ids: set[str]) -> dict | None:
        if not isinstance(raw, dict):
            return None
        ident = _text(raw.get("id"), 64)
        agent_id = _text(raw.get("agent_id"), 64)
        goal = _text(raw.get("goal"), 4000)
        kind = _text(raw.get("kind"), 16)
        if not ident or not goal or agent_id not in agent_ids or kind not in {"schedule", "condition"}:
            return None
        every = _text(raw.get("every"), 8)
        at = _text(raw.get("at"), 5)
        when = _text(raw.get("when"), 16)
        source = _text(raw.get("source"), 240)
        threshold = raw.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            threshold = None
        if kind == "schedule" and every not in {"hour", "day", "weekday"}:
            return None
        if kind == "schedule" and every in {"day", "weekday"} and not _clock(at):
            return None
        if kind == "condition" and when not in {"document", "drawdown", "margin"}:
            return None
        if kind == "condition" and when == "document" and not source:
            return None
        if kind == "condition" and when != "document" and threshold is None:
            return None
        steps = [_text(step, 240) for step in (raw.get("steps") if isinstance(raw.get("steps"), list) else [])[:8]]
        steps = [step for step in steps if step]
        return {
            "id": ident,
            "agent_id": agent_id,
            "goal": goal,
            "steps": steps,
            "enabled": raw.get("enabled") is not False,
            "kind": kind,
            "every": every if kind == "schedule" else None,
            "at": at if kind == "schedule" and every in {"day", "weekday"} else None,
            "time_zone": _text(raw.get("time_zone"), 64) or "UTC",
            "seen": _text(raw.get("seen"), 64),
            "when": when if kind == "condition" else None,
            "threshold": None if threshold is None else float(threshold),
            "source": source,
            "next_at": _text(raw.get("next_at"), 40),
            "last_at": _text(raw.get("last_at"), 40) or None,
            "last_error": _text(raw.get("last_error"), 240),
            "created_at": _text(raw.get("created_at"), 40),
            "updated_at": _text(raw.get("updated_at"), 40) or _text(raw.get("created_at"), 40),
        }


app_state_service = AppStateService()

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.models import AuditLog
from app.core.enums import ActorType, AuditEvent


class AuditService:
    async def log(
        self,
        session: AsyncSession,
        *,
        event: AuditEvent | str,
        actor_type: ActorType | str,
        actor_id: str | None = None,
        organization_id: str | None = None,
        run_id: str | None = None,
        action_id: str | None = None,
        tool: str | None = None,
        result: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            id=new_id("evt"),
            event=getattr(event, "value", event),
            actor_type=getattr(actor_type, "value", actor_type),
            actor_id=actor_id,
            organization_id=organization_id,
            run_id=run_id,
            action_id=action_id,
            tool=tool,
            result=result,
            payload=payload,
        )
        session.add(entry)
        await session.flush()
        return entry


audit_service = AuditService()

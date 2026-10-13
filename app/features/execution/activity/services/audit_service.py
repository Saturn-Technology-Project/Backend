from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.models import AuditLog
from app.core.enums import ActorType, AuditEvent
from app.features.execution.activity.services.zero_access import CONTENT_KEYS, protect_mapping


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
        record_id = new_id("evt")
        stored = payload
        if isinstance(payload, dict) and organization_id:
            content = {key: payload[key] for key in CONTENT_KEYS if key in payload and payload[key] not in (None, {}, [])}
            if content:
                rest = {key: value for key, value in payload.items() if key not in content}
                envelope = await protect_mapping(session, organization_id, record_id, "audit", content)
                if envelope is None:
                    rest["encryption_status"] = "unavailable"
                else:
                    rest["envelope"] = envelope
                    rest["encryption_status"] = "encrypted"
                    rest["org_key_version"] = envelope["org_key_version"]
                stored = rest
        entry = AuditLog(
            id=record_id,
            event=getattr(event, "value", event),
            actor_type=getattr(actor_type, "value", actor_type),
            actor_id=actor_id,
            organization_id=organization_id,
            run_id=run_id,
            action_id=action_id,
            tool=tool,
            result=result,
            payload=stored,
        )
        session.add(entry)
        await session.flush()
        from app.features.identity.auth.services.notices import notify_audit

        await notify_audit(session, entry)
        return entry


audit_service = AuditService()

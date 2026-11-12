"""Audit records for changes people make to what agents may do.

Tool calls and approvals were always recorded. Changes to the rules themselves were not:
a policy could be edited, a tool set reassigned or a member promoted with no trace. These
records carry ids, names and before/after states only, never tool content.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ActorType, AuditEvent
from app.features.execution.activity.services.audit_service import audit_service

CHANGE_SOURCE = "console"


async def record_change(
    session: AsyncSession,
    user: Any,
    event: AuditEvent,
    *,
    result: str = "",
    **details: Any,
) -> None:
    await audit_service.log(
        session,
        event=event,
        actor_type=ActorType.USER,
        actor_id=user.id,
        organization_id=user.organization_id,
        result=result or None,
        payload={"source": CHANGE_SOURCE, **{key: value for key, value in details.items() if value is not None}},
    )

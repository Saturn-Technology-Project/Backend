from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.identity.auth.models.user import User
from app.features.identity.auth.services.mailer import send_notice
from app.models import Agent, AuditLog

logger = logging.getLogger(__name__)

_KIND = {
    "approval_requested": "approval",
    "tool_executed": "execute",
    "tool_requested": "audit",
}


async def notify_audit(session: AsyncSession, entry: AuditLog) -> None:
    kind = _KIND.get(str(entry.event or ""))
    if kind is None or not entry.organization_id:
        return
    column = {
        "approval": User.notify_approval,
        "execute": User.notify_execute,
        "audit": User.notify_audit,
    }[kind]
    rows = await session.execute(
        select(User).where(
            User.organization_id == entry.organization_id,
            User.status == "active",
            column.is_(True),
        )
    )
    users = list(rows.scalars())
    if not users:
        return
    agent_name = ""
    if entry.actor_id:
        agent = await session.get(Agent, entry.actor_id)
        if agent is not None and agent.organization_id == entry.organization_id:
            agent_name = agent.name
    for user in users:
        try:
            await send_notice(
                user.email,
                kind=kind,
                tool=entry.tool or "",
                agent=agent_name,
                link=f"{_activity_link(entry.id)}",
                locale=user.locale,
            )
        except Exception:
            logger.exception("Could not send %s notice to %s", kind, user.email)


def _activity_link(event_id: str) -> str:
    from app.features.identity.auth.services.mailer import console_origin

    return f"{console_origin()}/activity?event={event_id}"

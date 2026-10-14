from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select

from app.core.ids import utcnow
from app.features.identity.auth.controllers.deps import AgentsRead, CurrentUser, SessionDep
from app.features.organization.org.services.plans import audit_retention_days
from app.features.execution.activity.services.audit_service import audit_service
from app.features.execution.activity.services.zero_access import public_audit_payload
from app.core.enums import ActorType, AuditEvent
from app.models import AuditLog, Organization
from app.schemas import ActivityEventRead

router = APIRouter(prefix="/activity", tags=["activity"])


def recorded_by_saturn_mcp(entry: AuditLog) -> bool:
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    return payload.get("source") == "saturn_mcp"


def _read(item: AuditLog) -> ActivityEventRead:
    payload = item.payload if isinstance(item.payload, dict) else None
    return ActivityEventRead(
        id=item.id,
        event=item.event,
        actor_type=item.actor_type,
        actor_id=item.actor_id,
        run_id=item.run_id,
        action_id=item.action_id,
        tool=item.tool,
        result=item.result,
        payload=public_audit_payload(payload),
        created_at=item.created_at,
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _matches(item: AuditLog, q: str | None, tool: str | None, agent: str | None) -> bool:
    if tool and tool.strip() and tool.strip() not in (item.tool or ""):
        return False
    if agent and agent.strip() and agent.strip() not in (item.actor_id or ""):
        return False
    needle = (q or "").strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        part
        for part in (item.event, item.tool, item.result, item.actor_id, item.actor_type)
        if part
    ).lower()
    return needle in haystack


async def _rows(
    session,
    user,
    *,
    run_id: str | None,
    q: str | None,
    tool: str | None,
    agent: str | None,
) -> list[AuditLog]:
    org = await session.get(Organization, user.organization_id)
    plan = org.plan if org is not None else None
    days = audit_retention_days(plan)
    stmt = (
        select(AuditLog)
        .where(AuditLog.organization_id == user.organization_id)
        .order_by(AuditLog.created_at.desc())
    )
    if run_id:
        stmt = stmt.where(AuditLog.run_id == run_id)
    result = await session.execute(stmt)
    rows = [item for item in result.scalars().all() if recorded_by_saturn_mcp(item)]
    if days is not None:
        cutoff = _aware(utcnow() - timedelta(days=days))
        rows = [item for item in rows if _aware(item.created_at) >= cutoff]
    if any(value and value.strip() for value in (q, tool, agent)):
        rows = [item for item in rows if _matches(item, q, tool, agent)]
    return rows


@router.get("", response_model=list[ActivityEventRead])
async def list_activity(
    session: SessionDep,
    user: CurrentUser,
    _access: AgentsRead,
    run_id: str | None = None,
    q: str | None = None,
    tool: str | None = None,
    agent: str | None = None,
):
    rows = await _rows(session, user, run_id=run_id, q=q, tool=tool, agent=agent)
    return [_read(item) for item in rows]


@router.get("/export")
async def export_activity(
    session: SessionDep,
    user: CurrentUser,
    _access: AgentsRead,
    run_id: str | None = None,
    q: str | None = None,
    tool: str | None = None,
    agent: str | None = None,
):
    rows = await _rows(session, user, run_id=run_id, q=q, tool=tool, agent=agent)
    body = json.dumps(
        [_read(item).model_dump(mode="json") for item in rows],
        ensure_ascii=False,
    )
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="activity.json"'},
    )


@router.post("/{event_id}/reveal")
async def reveal_activity(
    event_id: str,
    session: SessionDep,
    user: CurrentUser,
    _access: AgentsRead,
):
    entry = await session.get(AuditLog, event_id)
    if entry is None or entry.organization_id != user.organization_id or not recorded_by_saturn_mcp(entry):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Record not found")
    payload = entry.payload if isinstance(entry.payload, dict) else {}
    envelope = payload.get("envelope") if isinstance(payload.get("envelope"), dict) else None
    if envelope is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Record content is not encrypted")
    await audit_service.log(
        session,
        event=AuditEvent.CONTENT_REVEALED,
        actor_type=ActorType.USER,
        actor_id=user.id,
        organization_id=user.organization_id,
        run_id=entry.run_id,
        action_id=entry.action_id,
        tool=entry.tool,
        result="ciphertext",
        payload={"event_id": entry.id, "source": "saturn_mcp", "org_key_version": envelope.get("org_key_version")},
    )
    return {"envelope": envelope, "decrypt": "client"}

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ActorType, AuditEvent
from app.core.ids import utcnow
from app.features.execution.activity.services.audit_service import audit_service
from app.features.execution.approvals.models.approval import Approval, ApprovalPolicy, ApprovalWindow
from app.features.webhooks.services.webhook_service import publish_approval
from app.models import Action, Run

DEFAULT_TTL_MINUTES = 24 * 60
MAX_TTL_MINUTES = 7 * 24 * 60


def clamp_minutes(minutes: int) -> int:
    return max(1, min(int(minutes), MAX_TTL_MINUTES))


def check_minutes(minutes: int) -> int:
    value = int(minutes)
    if value < 1 or value > MAX_TTL_MINUTES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Expiration must be from 1 minute to 7 days",
        )
    return value


def _text(value) -> str:
    if value is None:
        return ""
    return value.value if hasattr(value, "value") else str(value)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def window_minutes(session: AsyncSession, organization_id: str) -> tuple[int, int]:
    row = await session.get(ApprovalWindow, organization_id)
    if row is None:
        return DEFAULT_TTL_MINUTES, DEFAULT_TTL_MINUTES
    return clamp_minutes(row.pending_ttl_minutes), clamp_minutes(row.resubmit_ttl_minutes)


async def save_window(
    session: AsyncSession,
    organization_id: str,
    *,
    pending_ttl_minutes: int | None = None,
    resubmit_ttl_minutes: int | None = None,
) -> ApprovalWindow:
    pending, resubmit = await window_minutes(session, organization_id)
    if pending_ttl_minutes is not None:
        pending = check_minutes(pending_ttl_minutes)
    if resubmit_ttl_minutes is not None:
        resubmit = check_minutes(resubmit_ttl_minutes)
    row = await session.get(ApprovalWindow, organization_id)
    if row is None:
        row = ApprovalWindow(
            organization_id=organization_id,
            pending_ttl_minutes=pending,
            resubmit_ttl_minutes=resubmit,
            updated_at=utcnow(),
        )
        session.add(row)
    else:
        row.pending_ttl_minutes = pending
        row.resubmit_ttl_minutes = resubmit
        row.updated_at = utcnow()
    await session.flush()
    return row


async def arm_pending(session: AsyncSession, approval: Approval, organization_id: str) -> None:
    if approval.expires_at is not None:
        return
    pending, _resubmit = await window_minutes(session, organization_id)
    approval.expires_at = utcnow() + timedelta(minutes=pending)


async def arm_resubmit(session: AsyncSession, approval: Approval, organization_id: str) -> None:
    minutes = None
    if approval.approval_policy_id:
        policy = await session.get(ApprovalPolicy, approval.approval_policy_id)
        if (
            policy is not None
            and policy.organization_id == organization_id
            and policy.resubmit_minutes
        ):
            minutes = clamp_minutes(int(policy.resubmit_minutes))
    if minutes is None:
        _pending, minutes = await window_minutes(session, organization_id)
    approval.resubmit_expires_at = utcnow() + timedelta(minutes=minutes)


async def notify_transition(
    session: AsyncSession,
    organization_id: str,
    event_type: str,
    approval: Approval,
    tool: str,
    phase: str | None = None,
) -> None:
    if not organization_id:
        return
    await publish_approval(
        session,
        organization_id=organization_id,
        event_type=event_type,
        approval_id=approval.id,
        run_id=approval.run_id,
        action_id=approval.action_id,
        tool=tool or "",
        phase=phase,
        expires_at=approval.expires_at,
        resubmit_expires_at=approval.resubmit_expires_at,
        approval_status=_text(approval.status),
    )


async def expire_if_due(session: AsyncSession, approval: Approval, now: datetime) -> str | None:
    current = _text(approval.status)
    if current == "expired":
        return approval.expired_phase or "pending"
    phase = None
    if current == "pending":
        deadline = _as_utc(approval.expires_at)
        if deadline is not None and deadline <= now:
            phase = "pending"
    elif current == "approved" and (approval.execution_state or "none") == "none":
        deadline = _as_utc(approval.resubmit_expires_at)
        if deadline is not None and deadline <= now:
            phase = "resubmit"
    if phase is None:
        return None
    values: dict = {"status": "expired", "expired_phase": phase}
    if phase == "pending":
        values["resolved_at"] = now
    result = await session.execute(
        update(Approval)
        .where(
            Approval.id == approval.id,
            Approval.status == current,
            Approval.execution_state == "none",
        )
        .values(**values)
    )
    if result.rowcount != 1:
        await session.refresh(approval)
        if _text(approval.status) == "expired":
            return approval.expired_phase or phase
        return None
    approval.status = "expired"
    approval.expired_phase = phase
    if phase == "pending":
        approval.resolved_at = now
    await _record_expiry(session, approval, phase, now)
    return phase


EXPIRE_BATCH = 500


async def expire_due(
    session: AsyncSession,
    organization_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """Expire approvals whose deadline has passed. At most EXPIRE_BATCH of each kind per call.

    The deadline is compared in the query. The worker calls this every few seconds; loading
    every open approval of every organization to compare dates in Python did not scale.
    """
    moment = now or utcnow()
    pending = select(Approval).where(
        Approval.status == "pending",
        Approval.expires_at.is_not(None),
        Approval.expires_at <= moment,
    )
    waiting = select(Approval).where(
        Approval.status == "approved",
        Approval.execution_state == "none",
        Approval.resubmit_expires_at.is_not(None),
        Approval.resubmit_expires_at <= moment,
    )
    if organization_id:
        pending = pending.join(Run, Approval.run_id == Run.id).where(Run.organization_id == organization_id)
        waiting = waiting.join(Run, Approval.run_id == Run.id).where(Run.organization_id == organization_id)
    changed = 0
    for approval in list(await session.scalars(pending.order_by(Approval.expires_at).limit(EXPIRE_BATCH))):
        if await expire_if_due(session, approval, moment):
            changed += 1
    for approval in list(await session.scalars(waiting.order_by(Approval.resubmit_expires_at).limit(EXPIRE_BATCH))):
        if await expire_if_due(session, approval, moment):
            changed += 1
    return changed


async def _record_expiry(session: AsyncSession, approval: Approval, phase: str, now: datetime) -> None:
    action = await session.get(Action, approval.action_id)
    run = await session.get(Run, approval.run_id)
    if action is not None and isinstance(action.arguments, dict) and "sealed" in action.arguments:
        action.arguments = {}
    if action is not None and _text(action.status) == "waiting_approval":
        action.status = "denied"
    if run is not None and _text(run.status) in {"waiting_approval", "running", "queued"}:
        run.status = "cancelled"
        run.updated_at = now
    organization_id = run.organization_id if run is not None else ""
    tool = action.tool if action is not None else ""
    mapped = action is not None and action.policy_reason in {"tool_set_approval", "saturn_mcp"}
    if not organization_id:
        return
    await audit_service.log(
        session,
        event=AuditEvent.APPROVAL_EXPIRED,
        actor_type=ActorType.SYSTEM,
        actor_id="saturn",
        organization_id=organization_id,
        run_id=approval.run_id,
        action_id=approval.action_id,
        tool=tool,
        result="expired",
        payload={
            "approval_id": approval.id,
            "phase": phase,
            "reason": "APPROVAL_EXPIRED",
            **({"source": "saturn_mcp"} if mapped else {}),
        },
    )
    await notify_transition(session, organization_id, "approval.expired", approval, tool, phase)

from __future__ import annotations

import json

from fastapi import APIRouter, Request

from app.features.identity.auth.controllers.deps import ActorDep, ApprovalsRead, ApprovalsWrite, CurrentUser, SessionDep
from app.schemas import ApprovalDecisionRead, ApprovalRead, ApprovalRequirementProgress
from app.features.execution.approvals.services.approval_service import approval_service

router = APIRouter(prefix="/approvals", tags=["approvals"])


async def serialize_approval(session, approval) -> ApprovalRead:
    amount = approval.requested_amount
    decisions = list(getattr(approval, "decisions", None) or [])
    approved_count = sum(1 for item in decisions if item.decision == "approved")
    status = approval.status.value if hasattr(approval.status, "value") else approval.status
    progress = await approval_service.requirement_progress(session, approval)
    return ApprovalRead(
        id=approval.id,
        run_id=approval.run_id,
        action_id=approval.action_id,
        status=status,
        requested_amount=float(amount) if amount is not None else None,
        resolved_by=approval.resolved_by,
        created_at=approval.created_at,
        resolved_at=approval.resolved_at,
        required_count=int(approval.required_count or 1),
        approved_count=approved_count,
        requirements=[ApprovalRequirementProgress(**item) for item in progress],
        execution_state=approval.execution_state or "none",
        expires_at=approval.expires_at,
        resubmit_expires_at=approval.resubmit_expires_at,
        expired_phase=approval.expired_phase,
    )


def _tool_result(action) -> str:
    if isinstance(action.result, dict):
        return str(action.result.get("status", "success"))
    return "success" if action.status == "completed" else action.status


@router.get("", response_model=list[ApprovalRead])
async def list_approvals(session: SessionDep, user: CurrentUser, _access: ApprovalsRead):
    approvals = await approval_service.list_approvals(session, user.organization_id)
    return [await serialize_approval(session, item) for item in approvals]


@router.get("/{approval_id}", response_model=ApprovalRead)
async def get_approval(
    approval_id: str, session: SessionDep, user: CurrentUser, _access: ApprovalsRead
):
    approval = await approval_service.get_approval(session, approval_id, user.organization_id)
    return await serialize_approval(session, approval)


@router.post("/{approval_id}/approve", response_model=ApprovalDecisionRead)
async def approve(
    approval_id: str,
    request: Request,
    session: SessionDep,
    actor_id: ActorDep,
    user: CurrentUser,
    _access: ApprovalsWrite,
):
    execution_arguments = None
    raw = await request.body()
    if raw:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("arguments"), dict):
            execution_arguments = parsed["arguments"]
    _run, action, outcome, detail = await approval_service.approve(
        session,
        approval_id,
        actor_id,
        organization_id=user.organization_id,
        execution_arguments=execution_arguments,
    )
    return ApprovalDecisionRead(
        status=outcome,
        action=action.tool,
        result=detail if detail is not None else _tool_result(action),
    )


@router.post("/{approval_id}/reject", response_model=ApprovalDecisionRead)
async def reject(
    approval_id: str, session: SessionDep, actor_id: ActorDep, user: CurrentUser, _access: ApprovalsWrite
):
    _run, action = await approval_service.reject(
        session, approval_id, actor_id, organization_id=user.organization_id
    )
    return ApprovalDecisionRead(
        status="rejected",
        action=action.tool,
        result="rejected",
    )


@router.post("/{approval_id}/cancel", response_model=ApprovalDecisionRead)
async def cancel(
    approval_id: str, session: SessionDep, actor_id: ActorDep, user: CurrentUser, _access: ApprovalsWrite
):
    _run, action = await approval_service.cancel(
        session, approval_id, actor_id, organization_id=user.organization_id
    )
    return ApprovalDecisionRead(status="cancelled", action=action.tool, result="cancelled")

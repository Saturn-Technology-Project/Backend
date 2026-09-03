from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ActorDep, ApprovalsRead, ApprovalsWrite, CurrentUser, SessionDep
from app.api.runs import serialize_run
from app.schemas import ApprovalDecisionRead, ApprovalRead
from app.services.agent_hub import agent_hub
from app.services.approval_service import approval_service

router = APIRouter(prefix="/approvals", tags=["approvals"])


def serialize_approval(approval) -> ApprovalRead:
    amount = approval.requested_amount
    return ApprovalRead(
        id=approval.id,
        run_id=approval.run_id,
        action_id=approval.action_id,
        status=approval.status,
        requested_amount=float(amount) if amount is not None else None,
        resolved_by=approval.resolved_by,
        created_at=approval.created_at,
        resolved_at=approval.resolved_at,
    )


def _tool_result(action) -> str:
    if isinstance(action.result, dict):
        return str(action.result.get("status", "success"))
    return "success" if action.status == "completed" else action.status


@router.get("", response_model=list[ApprovalRead])
async def list_approvals(session: SessionDep, user: CurrentUser, _access: ApprovalsRead):
    approvals = await approval_service.list_approvals(session, user.organization_id)
    return [serialize_approval(item) for item in approvals]


@router.get("/{approval_id}", response_model=ApprovalRead)
async def get_approval(
    approval_id: str, session: SessionDep, user: CurrentUser, _access: ApprovalsRead
):
    approval = await approval_service.get_approval(session, approval_id, user.organization_id)
    return serialize_approval(approval)


@router.post("/{approval_id}/approve", response_model=ApprovalDecisionRead)
async def approve(
    approval_id: str, session: SessionDep, actor_id: ActorDep, user: CurrentUser, _access: ApprovalsWrite
):
    run, action = await approval_service.approve(
        session, approval_id, actor_id, organization_id=user.organization_id
    )
    await agent_hub.broadcast_org(
        user.organization_id,
        {"type": "run.updated", "run": serialize_run(run).model_dump(mode="json")},
    )
    return ApprovalDecisionRead(
        status="completed",
        action=action.tool,
        result=_tool_result(action),
    )


@router.post("/{approval_id}/reject", response_model=ApprovalDecisionRead)
async def reject(
    approval_id: str, session: SessionDep, actor_id: ActorDep, user: CurrentUser, _access: ApprovalsWrite
):
    run, action = await approval_service.reject(
        session, approval_id, actor_id, organization_id=user.organization_id
    )
    await agent_hub.broadcast_org(
        user.organization_id,
        {"type": "run.updated", "run": serialize_run(run).model_dump(mode="json")},
    )
    return ApprovalDecisionRead(
        status="rejected",
        action=action.tool,
        result="rejected",
    )

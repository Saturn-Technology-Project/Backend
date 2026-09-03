from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.ids import utcnow
from app.models import Action, Agent, Approval, Run
from app.models.enums import (
    ActionStatus,
    ActorType,
    ApprovalStatus,
    AuditEvent,
    PolicyDecision,
    RunStatus,
)
from app.services.agent_hub import agent_hub
from app.services.audit_service import audit_service
from app.services.run_service import run_service

settings = get_settings()


class ApprovalService:
    async def list_approvals(self, session: AsyncSession, organization_id: str) -> list[Approval]:
        result = await session.execute(
            select(Approval)
            .join(Run, Approval.run_id == Run.id)
            .where(Run.organization_id == organization_id)
            .order_by(Approval.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_approval(
        self, session: AsyncSession, approval_id: str, organization_id: str
    ) -> Approval:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")
        run = await session.get(Run, approval.run_id)
        if run is None or run.organization_id != organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")
        return approval

    async def approve(
        self,
        session: AsyncSession,
        approval_id: str,
        actor_id: str,
        *,
        organization_id: str,
    ) -> tuple[Run, Action]:
        approval, action, run, agent = await self._load_pending(
            session, approval_id, actor_id, organization_id=organization_id
        )

        _tool, decision = await run_service.evaluate_action_policy(
            session, action, agent=agent
        )
        if decision.decision == PolicyDecision.DENY:
            approval.status = ApprovalStatus.REJECTED
            approval.resolved_at = utcnow()
            approval.resolved_by = actor_id
            action.status = ActionStatus.DENIED
            run.status = RunStatus.FAILED
            await session.flush()
            await audit_service.log(
                session,
                event=AuditEvent.RUN_FAILED,
                actor_type=ActorType.SYSTEM,
                actor_id="saturn",
                organization_id=run.organization_id,
                run_id=run.id,
                action_id=action.id,
                tool=action.tool,
                result="policy_deny_on_approve",
            )
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Policy no longer allows this action",
            )
        if decision.decision not in {
            PolicyDecision.ALLOW,
            PolicyDecision.APPROVAL_REQUIRED,
        }:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Policy decision is not executable")

        approval.status = ApprovalStatus.APPROVED
        approval.resolved_at = utcnow()
        approval.resolved_by = actor_id
        await session.flush()
        await audit_service.log(
            session,
            event=AuditEvent.APPROVAL_GRANTED,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            organization_id=run.organization_id,
            run_id=run.id,
            action_id=action.id,
            tool=action.tool,
            result="approved",
            payload={"approval_id": approval.id},
        )

        if agent_hub.is_connected(agent.id):
            run.status = RunStatus.RUNNING
            run.updated_at = utcnow()
            await session.flush()
            await agent_hub.send_command(
                agent.id,
                "approve",
                {
                    "run_id": run.id,
                    "approval_id": approval.id,
                    "actor_id": actor_id,
                },
            )
            return await run_service.load_run(session, run.id), action

        if not settings.in_process_runtime:
            run.status = RunStatus.RUNNING
            run.updated_at = utcnow()
            await session.flush()
            return await run_service.load_run(session, run.id), action

        await run_service.execute_action(
            session,
            action,
            agent=agent,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            approved=True,
            skip_policy=True,
        )
        if action.status == ActionStatus.FAILED:
            run.status = RunStatus.FAILED
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Tool execution failed")

        await run_service.continue_run(session, run)
        return await run_service.load_run(session, run.id), action

    async def reject(
        self,
        session: AsyncSession,
        approval_id: str,
        actor_id: str,
        *,
        organization_id: str,
    ) -> tuple[Run, Action]:
        approval, action, run, _agent = await self._load_pending(
            session, approval_id, actor_id, organization_id=organization_id
        )
        approval.status = ApprovalStatus.REJECTED
        approval.resolved_at = utcnow()
        approval.resolved_by = actor_id
        action.status = ActionStatus.REJECTED
        run.status = RunStatus.CANCELLED
        run.updated_at = utcnow()
        await session.flush()
        await audit_service.log(
            session,
            event=AuditEvent.APPROVAL_REJECTED,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            organization_id=run.organization_id,
            run_id=run.id,
            action_id=action.id,
            tool=action.tool,
            result="rejected",
            payload={"approval_id": approval.id},
        )
        if agent_hub.is_connected(run.agent_id):
            await agent_hub.send_command(
                run.agent_id,
                "reject",
                {
                    "run_id": run.id,
                    "approval_id": approval.id,
                    "actor_id": actor_id,
                },
            )
        return await run_service.load_run(session, run.id), action

    async def _load_pending(
        self,
        session: AsyncSession,
        approval_id: str,
        actor_id: str,
        organization_id: str,
    ) -> tuple[Approval, Action, Run, Agent]:
        if not actor_id:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User is required")

        result = await session.execute(
            select(Approval)
            .options(
                selectinload(Approval.action),
                selectinload(Approval.run).selectinload(Run.agent),
            )
            .where(Approval.id == approval_id)
        )
        approval = result.scalar_one_or_none()
        if approval is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")
        if approval.status != ApprovalStatus.PENDING:
            raise HTTPException(status.HTTP_409_CONFLICT, "Approval is not pending")

        action = approval.action or await session.get(Action, approval.action_id)
        if action is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Action not found")
        if action.status != ActionStatus.WAITING_APPROVAL:
            raise HTTPException(status.HTTP_409_CONFLICT, "Action is not waiting for approval")

        run = approval.run or await session.get(Run, approval.run_id)
        if run is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Run not found")
        if run.organization_id != organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found")

        agent = run.agent or await session.get(Agent, run.agent_id)
        if agent is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Agent not found")
        return approval, action, run, agent


approval_service = ApprovalService()

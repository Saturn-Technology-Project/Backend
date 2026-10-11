from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.ids import new_id, utcnow
from app.features.execution.approvals.models.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequirement
from app.features.organization.org.models.group import Group, GroupMember
from app.models import Action, Agent, Approval, Employee, Run
from app.core.enums import (
    ActionStatus,
    ActorType,
    ApprovalStatus,
    AuditEvent,
    PolicyDecision,
    RunStatus,
)
from app.features.execution.activity.services.audit_service import audit_service
from app.features.execution.runs.services.run_service import run_service

settings = get_settings()


class ApprovalService:
    async def list_approvals(self, session: AsyncSession, organization_id: str) -> list[Approval]:
        result = await session.execute(
            select(Approval)
            .options(selectinload(Approval.decisions))
            .join(Run, Approval.run_id == Run.id)
            .where(Run.organization_id == organization_id)
            .order_by(Approval.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_approval(
        self, session: AsyncSession, approval_id: str, organization_id: str
    ) -> Approval:
        result = await session.execute(
            select(Approval).options(selectinload(Approval.decisions)).where(Approval.id == approval_id)
        )
        approval = result.scalar_one_or_none()
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
    ) -> tuple[Run, Action, str, str | None]:
        approval, action, run, agent = await self._load_pending(
            session, approval_id, actor_id, organization_id=organization_id
        )
        if int(approval.required_count or 1) > 1:
            return await self._approve_quorum(session, approval, action, run, agent, actor_id)
        mapped = action.policy_reason in {"tool_set_approval", "saturn_mcp"}

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
                payload={"source": "saturn_mcp"} if mapped else None,
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
            payload={"approval_id": approval.id, **({"source": "saturn_mcp"} if mapped else {})},
        )

        if mapped or not settings.in_process_runtime:
            run.status = RunStatus.RUNNING
            run.updated_at = utcnow()
            await session.flush()
            return await run_service.load_run(session, run.id), action, "completed", None

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
        return await run_service.load_run(session, run.id), action, "completed", None

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
        if int(approval.required_count or 1) > 1:
            self._ensure_open(approval)
            employee = await self._employee(session, actor_id, organization_id)
            await self._assert_voter(session, approval, employee, actor_id)
        mapped = action.policy_reason in {"tool_set_approval", "saturn_mcp"}
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
            payload={"approval_id": approval.id, **({"source": "saturn_mcp"} if mapped else {})},
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

    async def cancel(
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
        approval.status = ApprovalStatus.CANCELLED
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
            result="cancelled",
            payload={"approval_id": approval.id},
        )
        return await run_service.load_run(session, run.id), action

    async def configure_request(
        self,
        session: AsyncSession,
        approval: Approval,
        *,
        organization_id: str,
        decision,
        arguments: dict,
        requester_user_id: str | None,
    ) -> None:
        approval.parameter_digest = parameter_digest(arguments if isinstance(arguments, dict) else {})
        approval.requester_user_id = requester_user_id
        approval.policy_id = getattr(decision, "policy_id", None)
        approval.policy_version = getattr(decision, "policy_version", None)
        approval.required_count = 1
        approval.allow_requester = True
        approval.execution_state = approval.execution_state or "none"
        policy_id = getattr(decision, "approval_policy_id", None)
        if not policy_id:
            return
        policy = await session.get(ApprovalPolicy, policy_id)
        if (
            policy is None
            or policy.organization_id != organization_id
            or policy.status != "active"
        ):
            return
        approval.approval_policy_id = policy.id
        approval.approval_policy_version = policy.version or 1
        approval.allow_requester = bool(policy.allow_requester)
        requirements = list(
            (
                await session.scalars(
                    select(ApprovalRequirement)
                    .where(ApprovalRequirement.approval_policy_id == policy.id)
                    .order_by(ApprovalRequirement.position)
                )
            ).all()
        )
        if requirements:
            approval.group_requirements = [
                {"group_id": item.group_id, "required_count": int(item.required_count)}
                for item in requirements
            ]
            approval.group_id = None
            approval.required_count = sum(int(item.required_count) for item in requirements)
        else:
            approval.group_requirements = []
            approval.required_count = int(policy.required_count or 1)
            approval.group_id = policy.group_id
        if policy.expires_minutes:
            approval.expires_at = utcnow() + timedelta(minutes=int(policy.expires_minutes))
        await session.flush()

    async def _approve_quorum(
        self,
        session: AsyncSession,
        approval: Approval,
        action: Action,
        run: Run,
        agent: Agent,
        actor_id: str,
    ) -> tuple[Run, Action, str, str | None]:
        self._ensure_open(approval)
        employee = await self._employee(session, actor_id, run.organization_id)
        await self._assert_voter(session, approval, employee, actor_id)
        duplicate = await session.scalar(
            select(ApprovalDecision).where(
                ApprovalDecision.approval_id == approval.id,
                ApprovalDecision.employee_id == employee.id,
            )
        )
        if duplicate is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "This member already decided")
        session.add(
            ApprovalDecision(
                id=new_id("vote"),
                approval_id=approval.id,
                employee_id=employee.id,
                decision="approved",
            )
        )
        await session.flush()
        counts = await self._tally(session, approval)
        progress = _progress_text(counts)
        await audit_service.log(
            session,
            event=AuditEvent.APPROVAL_GRANTED,
            actor_type=ActorType.USER,
            actor_id=actor_id,
            organization_id=run.organization_id,
            run_id=run.id,
            action_id=action.id,
            tool=action.tool,
            result="recorded",
            payload={
                "approval_id": approval.id,
                "employee_id": employee.id,
                "approved_count": counts[0][0] if len(counts) == 1 else progress,
                "required_count": int(approval.required_count or 1),
                "progress": progress,
                "source": "saturn_mcp",
            },
        )
        if not _requirements_met(counts):
            return run, action, "pending", progress

        await self._revalidate_quorum(session, approval, action, agent)
        counts = await self._tally(session, approval)
        progress = _progress_text(counts)
        if not _requirements_met(counts):
            return run, action, "pending", progress

        claimed = await session.execute(
            update(Approval)
            .where(
                Approval.id == approval.id,
                Approval.status == ApprovalStatus.PENDING.value,
                Approval.execution_state == "none",
            )
            .values(
                status=ApprovalStatus.APPROVED.value,
                execution_state="claimed",
                resolved_at=utcnow(),
                resolved_by=actor_id,
            )
        )
        if claimed.rowcount != 1:
            raise HTTPException(status.HTTP_409_CONFLICT, "Execution already claimed")
        approval.status = ApprovalStatus.APPROVED.value
        approval.execution_state = "claimed"
        await audit_service.log(
            session,
            event=AuditEvent.APPROVAL_QUORUM,
            actor_type=ActorType.SYSTEM,
            actor_id="saturn",
            organization_id=run.organization_id,
            run_id=run.id,
            action_id=action.id,
            tool=action.tool,
            result="reached",
            payload={
                "approval_id": approval.id,
                "required_count": int(approval.required_count or 1),
                "progress": progress,
                "source": "saturn_mcp",
            },
        )
        await audit_service.log(
            session,
            event=AuditEvent.EXECUTION_CLAIMED,
            actor_type=ActorType.SYSTEM,
            actor_id="saturn",
            organization_id=run.organization_id,
            run_id=run.id,
            action_id=action.id,
            tool=action.tool,
            result="claimed",
            payload={"approval_id": approval.id, "source": "saturn_mcp"},
        )
        try:
            await self._execute_claimed(session, approval, action, run, agent, actor_id)
        except HTTPException as exc:
            if approval.execution_state == "claimed":
                approval.execution_state = "uncertain" if _uncertain(exc) else "failed"
            raise
        return await run_service.load_run(session, run.id), action, "completed", "success"

    async def _execute_claimed(
        self,
        session: AsyncSession,
        approval: Approval,
        action: Action,
        run: Run,
        agent: Agent,
        actor_id: str,
    ) -> None:
        tool = await run_service.get_tool(session, agent.organization_id, action.tool)
        gateway = bool(getattr(tool, "mcp_server_id", None) or getattr(tool, "mcp_endpoint", None))
        if gateway or not settings.in_process_runtime:
            from app.features.mcp.saturn.services.saturn_mcp import saturn_mcp

            arguments = action.arguments if isinstance(action.arguments, dict) else {}
            try:
                raw_response = await saturn_mcp._proxy(session, tool, arguments)
            except Exception as exc:
                if _uncertain(exc):
                    approval.execution_state = "uncertain"
                    await session.flush()
                    from app.features.identity.auth.services.notices import notify_task_outcome

                    await notify_task_outcome(session, run, "failed")
                    raise HTTPException(
                        status.HTTP_409_CONFLICT, "Upstream outcome is uncertain"
                    ) from exc
                approval.execution_state = "failed"
                action.status = ActionStatus.FAILED
                run.status = RunStatus.FAILED
                await session.flush()
                raise
            from app.features.mcp.saturn.services.saturn_mcp import _split_remote

            raw_response, result = _split_remote(raw_response)
            action.status = ActionStatus.COMPLETED.value
            action.result = result
            approval.execution_state = "executed"
            run.status = RunStatus.COMPLETED.value
            run.updated_at = utcnow()
            await session.flush()
            await audit_service.log(
                session,
                event=AuditEvent.TOOL_EXECUTED,
                actor_type=ActorType.AGENT,
                actor_id=agent.id,
                organization_id=run.organization_id,
                run_id=run.id,
                action_id=action.id,
                tool=action.tool,
                result="completed",
                payload={
                    "approval_id": approval.id,
                    "arguments": arguments,
                    "server_result": result,
                    "raw_response": raw_response,
                    "source": "saturn_mcp",
                },
            )
            await audit_service.log(
                session,
                event=AuditEvent.RUN_COMPLETED,
                actor_type=ActorType.SYSTEM,
                actor_id="saturn",
                organization_id=run.organization_id,
                run_id=run.id,
                result="success",
            )
            return
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
            approval.execution_state = "failed"
            run.status = RunStatus.FAILED
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Tool execution failed")
        approval.execution_state = "executed"
        await run_service.continue_run(session, run)

    async def _revalidate_quorum(
        self,
        session: AsyncSession,
        approval: Approval,
        action: Action,
        agent: Agent,
    ) -> None:
        arguments = action.arguments if isinstance(action.arguments, dict) else {}
        if approval.parameter_digest and approval.parameter_digest != parameter_digest(arguments):
            await self._invalidate(session, approval, action, "parameters changed")
        _tool, fresh = await run_service.evaluate_action_policy(session, action, agent=agent)
        if fresh.decision == PolicyDecision.DENY:
            approval.status = ApprovalStatus.REJECTED
            approval.resolved_at = utcnow()
            action.status = ActionStatus.DENIED
            run = await session.get(Run, approval.run_id)
            if run is not None:
                run.status = RunStatus.FAILED
            await session.flush()
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Policy no longer allows this action")
        if approval.policy_version is not None and fresh.policy_version != approval.policy_version:
            await self._invalidate(session, approval, action, "policy changed")
        if approval.approval_policy_id:
            policy = await session.get(ApprovalPolicy, approval.approval_policy_id)
            if (
                policy is None
                or policy.status != "active"
                or (policy.version or 1) != (approval.approval_policy_version or 1)
            ):
                await self._invalidate(session, approval, action, "approval policy changed")
        await audit_service.log(
            session,
            event=AuditEvent.POLICY_EVALUATED,
            actor_type=ActorType.SYSTEM,
            actor_id="policy_engine",
            organization_id=agent.organization_id,
            run_id=action.run_id,
            action_id=action.id,
            tool=action.tool,
            result="revalidated",
            payload={"approval_id": approval.id, "policy_version": fresh.policy_version, "source": "saturn_mcp"},
        )

    async def _invalidate(self, session: AsyncSession, approval: Approval, action: Action, reason: str) -> None:
        approval.status = ApprovalStatus.INVALIDATED
        approval.resolved_at = utcnow()
        action.status = ActionStatus.DENIED
        run = await session.get(Run, approval.run_id)
        if run is not None:
            run.status = RunStatus.FAILED
            await audit_service.log(
                session,
                event=AuditEvent.APPROVAL_INVALIDATED,
                actor_type=ActorType.SYSTEM,
                actor_id="saturn",
                organization_id=run.organization_id,
                run_id=run.id,
                action_id=action.id,
                tool=action.tool,
                result="invalidated",
                payload={"approval_id": approval.id, "reason": reason},
            )
        await session.flush()
        raise HTTPException(status.HTTP_409_CONFLICT, "Approval no longer matches this operation")

    def _ensure_open(self, approval: Approval) -> None:
        if approval.expires_at is not None and _as_utc(approval.expires_at) <= utcnow():
            approval.status = ApprovalStatus.EXPIRED
            approval.resolved_at = utcnow()
            raise HTTPException(status.HTTP_409_CONFLICT, "Approval expired")
        state = approval.execution_state or "none"
        if state in {"executed", "claimed", "uncertain", "failed"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "Approval is not pending")

    async def _employee(self, session: AsyncSession, user_id: str, organization_id: str) -> Employee:
        employee = await session.scalar(
            select(Employee).where(
                Employee.user_id == user_id,
                Employee.organization_id == organization_id,
            )
        )
        if employee is None or employee.status != "active":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not an eligible approver")
        return employee

    async def _assert_voter(
        self,
        session: AsyncSession,
        approval: Approval,
        employee: Employee,
        actor_id: str,
    ) -> None:
        if not approval.allow_requester and approval.requester_user_id == actor_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Requester cannot approve this request")
        specs = _requirement_specs(approval)
        if not specs:
            return
        member = await session.scalar(
            select(GroupMember).where(
                GroupMember.group_id.in_([item["group_id"] for item in specs]),
                GroupMember.employee_id == employee.id,
            )
        )
        if member is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not an eligible approver")

    async def requirement_progress(self, session: AsyncSession, approval: Approval) -> list[dict]:
        specs = _requirement_specs(approval)
        if len(specs) < 2:
            return []
        counts = await self._tally(session, approval)
        group_ids = [item["group_id"] for item in specs]
        groups = await session.execute(select(Group).where(Group.id.in_(group_ids)))
        names = {item.id: item.name for item in groups.scalars().all()}
        return [
            {
                "group_id": spec["group_id"],
                "group_name": names.get(spec["group_id"], ""),
                "required_count": need,
                "approved_count": got,
            }
            for spec, (got, need) in zip(specs, counts, strict=True)
        ]

    async def _tally(self, session: AsyncSession, approval: Approval) -> list[tuple[int, int]]:
        result = await session.execute(
            select(ApprovalDecision).where(
                ApprovalDecision.approval_id == approval.id,
                ApprovalDecision.decision == "approved",
            )
        )
        votes = list(result.scalars().all())
        specs = _requirement_specs(approval)
        if not specs:
            return [(len(votes), int(approval.required_count or 1))]
        rows = await session.execute(
            select(GroupMember).where(
                GroupMember.group_id.in_([item["group_id"] for item in specs]),
                GroupMember.employee_id.in_([item.employee_id for item in votes] or [""]),
            )
        )
        memberships: dict[str, set[str]] = {}
        for member in rows.scalars().all():
            memberships.setdefault(member.employee_id, set()).add(member.group_id)
        return [
            (
                sum(1 for item in votes if spec["group_id"] in memberships.get(item.employee_id, set())),
                int(spec["required_count"]),
            )
            for spec in specs
        ]


def _requirement_specs(approval: Approval) -> list[dict]:
    raw = approval.group_requirements if isinstance(approval.group_requirements, list) else []
    specs = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("group_id"):
            continue
        specs.append(
            {"group_id": str(item["group_id"]), "required_count": int(item.get("required_count") or 1)}
        )
    if specs:
        return specs
    if approval.group_id:
        return [{"group_id": approval.group_id, "required_count": int(approval.required_count or 1)}]
    return []


def _progress_text(counts: list[tuple[int, int]]) -> str:
    return "+".join(f"{got}/{need}" for got, need in counts)


def _requirements_met(counts: list[tuple[int, int]]) -> bool:
    return all(got >= need for got, need in counts)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def parameter_digest(arguments: dict) -> str:
    raw = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _uncertain(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or type(exc).__name__ in {
        "TimeoutException",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
    }


approval_service = ApprovalService()

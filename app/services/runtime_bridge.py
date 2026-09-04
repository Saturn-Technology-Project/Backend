from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ids import utcnow
from app.models import Action, Agent, Approval, Policy, Run, Tool
from app.models.enums import (
    ActionStatus,
    ActorType,
    ApprovalStatus,
    AuditEvent,
    RunStatus,
)
from app.services.agent_config import assigned_tool_names, public_settings, system_prompt
from app.services.audit_service import audit_service

_SKIP_AUDIT = {AuditEvent.RUN_CREATED.value, AuditEvent.AGENT_STARTED.value}

_STATUS = {
    "queued": RunStatus.QUEUED,
    "running": RunStatus.RUNNING,
    "waiting_approval": RunStatus.WAITING_APPROVAL,
    "completed": RunStatus.COMPLETED,
    "failed": RunStatus.FAILED,
    "rejected": RunStatus.CANCELLED,
    "cancelled": RunStatus.CANCELLED,
}


def _as_result(value: Any) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return {"data": value}


def _amount(arguments: dict) -> Decimal | None:
    amount = arguments.get("amount")
    if amount is None:
        return None
    return Decimal(str(amount))


async def runtime_payload(
    session: AsyncSession,
    *,
    agent: Agent,
    run: Run,
    actor_id: str,
) -> dict[str, Any]:
    tools = list(
        (
            await session.execute(
                select(Tool)
                .where(Tool.organization_id == agent.organization_id)
                .order_by(Tool.name.asc())
            )
        )
        .scalars()
        .all()
    )
    policies = list(
        (
            await session.execute(
                select(Policy)
                .options(selectinload(Policy.rules))
                .where(Policy.organization_id == agent.organization_id)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    catalog = [tool.name for tool in tools]
    settings = public_settings(agent)
    return {
        "run_id": run.id,
        "agent_id": agent.id,
        "input": run.input,
        "user_id": actor_id,
        "organization_id": agent.organization_id,
        "system_prompt": system_prompt(agent),
        "model": settings["model"],
        "provider": settings["provider"],
        "base_url": settings["base_url"],
        "temperature": settings["temperature"],
        "max_output": settings["max_output"],
        "response_style": settings["response_style"],
        "autonomy": settings["autonomy"],
        "tools": assigned_tool_names(agent.settings, catalog),
        "limits": {
            "max_concurrent_runs": settings["max_concurrent_runs"],
            "max_tool_calls": settings["max_tool_calls"],
            "max_runtime_minutes": settings["max_runtime_minutes"],
        },
        "policies": [
            {
                "id": policy.id,
                "name": policy.name,
                "rules": [
                    {
                        "tool": rule.tool,
                        "condition": rule.condition,
                        "decision": rule.decision,
                        "policy_id": policy.id,
                    }
                    for rule in sorted(policy.rules, key=lambda item: item.position)
                ],
            }
            for policy in policies
        ],
    }


async def apply_runtime_result(
    session: AsyncSession,
    run: Run,
    payload: dict[str, Any],
) -> Run:
    status = _STATUS.get(str(payload.get("status") or "running"), RunStatus.RUNNING)
    run.status = status
    run.updated_at = utcnow()

    for position, item in enumerate(payload.get("actions") or []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        action_id = str(item["id"])
        action = await session.get(Action, action_id)
        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        if action is None:
            action = Action(
                id=action_id,
                run=run,
                tool=str(item.get("tool") or "unknown"),
                arguments=arguments,
                policy_decision=item.get("policy_decision"),
                status=str(item.get("status") or ActionStatus.PENDING),
                result=_as_result(item.get("result")),
                position=position,
            )
            session.add(action)
        else:
            action.tool = str(item.get("tool") or action.tool)
            action.arguments = arguments or action.arguments
            action.policy_decision = item.get("policy_decision")
            action.status = str(item.get("status") or action.status)
            action.result = _as_result(item.get("result"))
            action.position = position
            action.updated_at = utcnow()

    await session.flush()
    actions = list(run.actions)
    approval_id = payload.get("approval_id")
    if approval_id and status == RunStatus.WAITING_APPROVAL:
        existing = await session.get(Approval, str(approval_id))
        if existing is None:
            waiting = next(
                (
                    action
                    for action in actions
                    if str(action.status) == "waiting_approval"
                ),
                None,
            )
            if waiting is not None:
                approval = Approval(
                    id=str(approval_id),
                    run=run,
                    action=waiting,
                    status=ApprovalStatus.PENDING.value,
                    requested_amount=_amount(waiting.arguments),
                )
                session.add(approval)
                if approval not in run.approvals:
                    run.approvals.append(approval)
                waiting.policy_decision = waiting.policy_decision or "approval_required"

    await session.flush()

    for event in payload.get("audit") or []:
        if not isinstance(event, dict):
            continue
        name = str(event.get("event") or "")
        if not name or name in _SKIP_AUDIT:
            continue
        action_id = event.get("action_id")
        if action_id and await session.get(Action, str(action_id)) is None:
            action_id = None
        await audit_service.log(
            session,
            event=name,
            actor_type=event.get("actor_type") or ActorType.AGENT,
            actor_id=event.get("actor_id"),
            organization_id=run.organization_id,
            run_id=run.id,
            action_id=action_id,
            tool=event.get("tool"),
            result=event.get("result"),
            payload=event.get("payload") if isinstance(event.get("payload"), dict) else None,
        )

    if status == RunStatus.COMPLETED and not any(
        event.get("event") == AuditEvent.RUN_COMPLETED.value
        for event in (payload.get("audit") or [])
        if isinstance(event, dict)
    ):
        await audit_service.log(
            session,
            event=AuditEvent.RUN_COMPLETED,
            actor_type=ActorType.SYSTEM,
            actor_id="saturn",
            organization_id=run.organization_id,
            run_id=run.id,
            result="success",
        )

    await session.flush()
    return run

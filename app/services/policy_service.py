from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ids import new_id, utcnow
from app.models import Agent, Policy, PolicyRule
from app.models.associations import agent_policies
from app.schemas import PolicyCreate, PolicyRead, PolicyRuleIn, PolicyRuleRead, PolicyUpdate
from app.services.policy_engine import compile_condition, normalize_decision, parse_condition_string


def _slug_id(prefix: str, name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return f"{prefix}_{slug}" if slug else new_id(prefix)


def _rule_payload(rule: PolicyRuleIn, fallback_tool: str | None) -> tuple[str, str, list[dict], str, int | None]:
    try:
        decision = normalize_decision(rule.decision).value
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    structured = [item.model_dump() for item in rule.conditions] if rule.conditions else []
    condition = (rule.condition or "").strip()
    if structured:
        condition = compile_condition(structured)
    elif condition:
        structured = parse_condition_string(condition)
    else:
        condition = "true"
    tool = (rule.tool or fallback_tool or "").strip()
    if not tool:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Each rule needs a tool")
    return tool, condition, structured, decision, rule.priority


def serialize_rule(rule: PolicyRule) -> PolicyRuleRead:
    structured = rule.conditions if isinstance(rule.conditions, list) else []
    if not structured:
        structured = parse_condition_string(rule.condition)
    return PolicyRuleRead(
        id=rule.id,
        tool=rule.tool,
        condition=rule.condition,
        conditions=structured,
        decision=rule.decision,
        position=rule.position,
    )


def serialize_policy(policy: Policy) -> PolicyRead:
    rules = sorted(policy.rules, key=lambda item: item.position)
    return PolicyRead(
        id=policy.id,
        name=policy.name,
        description=policy.description or "",
        status=policy.status or "active",
        version=policy.version or 1,
        environment=policy.environment or "production",
        agent_ids=[agent.id for agent in policy.agents],
        rules=[serialize_rule(rule) for rule in rules],
        created_at=policy.created_at,
        updated_at=policy.updated_at,
    )


class PolicyService:
    async def list_policies(self, session: AsyncSession, organization_id: str) -> list[Policy]:
        result = await session.execute(
            select(Policy)
            .options(selectinload(Policy.rules), selectinload(Policy.agents))
            .where(Policy.organization_id == organization_id)
            .order_by(Policy.created_at.asc())
        )
        return list(result.scalars().unique().all())

    async def get_policy(
        self, session: AsyncSession, policy_id: str, organization_id: str
    ) -> Policy:
        result = await session.execute(
            select(Policy)
            .options(selectinload(Policy.rules), selectinload(Policy.agents))
            .where(Policy.id == policy_id)
        )
        policy = result.scalar_one_or_none()
        if policy is None or policy.organization_id != organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Policy not found")
        return policy

    async def create_policy(
        self, session: AsyncSession, organization_id: str, body: PolicyCreate
    ) -> Policy:
        ident = body.id or _slug_id("policy", body.name)
        if await session.get(Policy, ident) is not None:
            ident = new_id("policy")
        policy = Policy(
            id=ident,
            organization_id=organization_id,
            name=body.name.strip(),
            description=(body.description or "").strip(),
            status=(body.status or "active").strip().lower(),
            version=1,
            environment=(body.environment or "production").strip().lower(),
        )
        session.add(policy)
        await session.flush()
        await self._set_agents(session, policy.id, organization_id, body.agent_ids)
        await self._replace_rules(session, policy.id, body.rules, body.tool)
        await session.flush()
        return await self.get_policy(session, policy.id, organization_id)

    async def update_policy(
        self, session: AsyncSession, policy: Policy, body: PolicyUpdate
    ) -> Policy:
        changed = False
        if body.name is not None:
            policy.name = body.name.strip()
            changed = True
        if body.description is not None:
            policy.description = body.description.strip()
            changed = True
        if body.status is not None:
            policy.status = body.status.strip().lower()
            changed = True
        if body.environment is not None:
            policy.environment = body.environment.strip().lower()
            changed = True
        if body.agent_ids is not None:
            await self._set_agents(session, policy.id, policy.organization_id, body.agent_ids)
            changed = True
        if body.rules is not None:
            await self._replace_rules(session, policy.id, body.rules, body.tool)
            changed = True
        if changed:
            policy.version = (policy.version or 1) + 1
            policy.updated_at = utcnow()
            await session.flush()
        return await self.get_policy(session, policy.id, policy.organization_id)

    async def _set_agents(
        self,
        session: AsyncSession,
        policy_id: str,
        organization_id: str,
        agent_ids: list[str],
    ) -> None:
        await session.execute(delete(agent_policies).where(agent_policies.c.policy_id == policy_id))
        agents = await self._load_agents(session, organization_id, agent_ids)
        if agents:
            await session.execute(
                insert(agent_policies),
                [{"agent_id": agent.id, "policy_id": policy_id} for agent in agents],
            )

    async def _load_agents(
        self, session: AsyncSession, organization_id: str, agent_ids: list[str]
    ) -> list[Agent]:
        if not agent_ids:
            return []
        result = await session.execute(
            select(Agent).where(
                Agent.organization_id == organization_id,
                Agent.id.in_(agent_ids),
            )
        )
        return list(result.scalars().all())

    async def _replace_rules(
        self,
        session: AsyncSession,
        policy_id: str,
        rules: list[PolicyRuleIn],
        fallback_tool: str | None,
    ) -> None:
        await session.execute(delete(PolicyRule).where(PolicyRule.policy_id == policy_id))
        await session.flush()
        for index, rule in enumerate(rules):
            tool, condition, structured, decision, priority = _rule_payload(rule, fallback_tool)
            session.add(
                PolicyRule(
                    id=rule.id or new_id("rule"),
                    policy_id=policy_id,
                    tool=tool,
                    condition=condition,
                    conditions=structured or None,
                    decision=decision,
                    position=priority if priority is not None else index,
                )
            )


policy_service = PolicyService()

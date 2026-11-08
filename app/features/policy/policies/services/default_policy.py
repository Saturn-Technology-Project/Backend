"""Default tool policy for a new organization.

Read-class tools are allowed. Every other tool, including one whose permissions are
unknown, requires approval. Existing organizations are not rewritten; startup only logs
agents that have no policy.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ids import new_id
from app.models import Agent, Policy, PolicyRule, Tool

logger = logging.getLogger(__name__)

MARKER = "saturn-default-v1"
_READ = {"read"}


def decision_for_permissions(permissions: list | None) -> str:
    """Allow only tools that are read and nothing else. Unknown or empty fails closed."""
    values = {str(item).strip().lower() for item in (permissions or [])}
    if values and values <= _READ:
        return "allow"
    return "approval"


async def _default_policy(session: AsyncSession, organization_id: str) -> Policy | None:
    result = await session.execute(
        select(Policy)
        .options(selectinload(Policy.rules))
        .where(Policy.organization_id == organization_id, Policy.description == MARKER)
    )
    return result.scalar_one_or_none()


async def extend_default_policy(session: AsyncSession, organization_id: str) -> Policy | None:
    """Add rules for new tools only when onboarding already created the default policy."""
    policy = await _default_policy(session, organization_id)
    if policy is None:
        return None
    return await _fill_rules(session, policy)


async def ensure_default_policy(session: AsyncSession, organization_id: str) -> Policy:
    policy = await _default_policy(session, organization_id)
    if policy is None:
        policy = Policy(
            id=new_id("policy"),
            organization_id=organization_id,
            name="Default tool access",
            description=MARKER,
            status="active",
            version=1,
            environment="production",
        )
        session.add(policy)
        await session.flush()
        loaded = await _default_policy(session, organization_id)
        if loaded is None:
            raise RuntimeError("default policy was not stored")
        policy = loaded
    return await _fill_rules(session, policy)


async def _fill_rules(session: AsyncSession, policy: Policy) -> Policy:
    existing_rules = list(policy.rules or [])
    tools = list(
        (
            await session.scalars(
                select(Tool).where(Tool.organization_id == policy.organization_id, Tool.status == "active")
            )
        ).all()
    )
    covered = {rule.tool for rule in existing_rules}
    position = len(covered)
    for tool in tools:
        if tool.name in covered:
            continue
        session.add(
            PolicyRule(
                id=new_id("rule"),
                policy_id=policy.id,
                tool=tool.name,
                condition="true",
                decision=decision_for_permissions(tool.permissions),
                position=position,
            )
        )
        position += 1
        covered.add(tool.name)
    await session.flush()
    return policy


async def log_agents_without_policy(session: AsyncSession) -> list[str]:
    """Report agents the default-deny flag would block. Does not change them."""
    agents = list((await session.scalars(select(Agent))).all())
    result = await session.execute(
        select(Policy).options(selectinload(Policy.agents)).where(Policy.status == "active")
    )
    policies = list(result.scalars().unique().all())
    uncovered: list[str] = []
    for agent in agents:
        applicable = [
            policy
            for policy in policies
            if policy.organization_id == agent.organization_id
            and (not list(policy.agents or []) or any(item.id == agent.id for item in policy.agents))
        ]
        if not applicable:
            uncovered.append(f"{agent.organization_id}:{agent.id}")
    if uncovered:
        logger.warning("agents with no applicable policy: %s", ", ".join(sorted(uncovered)))
    return uncovered

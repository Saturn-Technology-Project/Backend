import pytest

from app.core.config import get_settings
from app.core.enums import PolicyDecision
from app.features.policy.policies.services.default_policy import (
    MARKER,
    decision_for_permissions,
    ensure_default_policy,
    log_agents_without_policy,
)
from app.features.policy.policies.services.policy_engine import EvaluationResult, merge_results
from app.models import Agent, Tool


def test_empty_merge_denies_when_the_flag_is_on():
    settings = get_settings()
    previous = settings.policy_default_deny
    settings.policy_default_deny = True
    try:
        result = merge_results([])
    finally:
        settings.policy_default_deny = previous
    assert result.decision == PolicyDecision.DENY
    assert result.reason == "沒有適用的 policy"


def test_allow_rule_still_wins():
    result = merge_results([EvaluationResult(decision=PolicyDecision.ALLOW, policy_id="policy_open")])
    assert result.decision == PolicyDecision.ALLOW


def test_write_permissions_require_approval():
    assert decision_for_permissions(["read"]) == "allow"
    assert decision_for_permissions(["write"]) == "approval"
    assert decision_for_permissions([]) == "allow"


async def _db_session():
    from app.db.database import get_session
    from app.main import app

    override = app.dependency_overrides[get_session]
    generator = override()
    session = await anext(generator)
    return generator, session


@pytest.mark.asyncio
async def test_onboarding_creates_a_default_policy(client):
    org_id = (await client.get("/auth/me")).json()["organization"]["id"]
    generator, session = await _db_session()
    try:
        session.add(
            Tool(
                id="tool_notes_read",
                organization_id=org_id,
                name="notes.read",
                permissions=["read"],
                status="active",
            )
        )
        session.add(
            Tool(
                id="tool_notes_write",
                organization_id=org_id,
                name="notes.write",
                permissions=["write"],
                status="active",
            )
        )
        await session.flush()
        policy = await ensure_default_policy(session, org_id)
        await session.commit()
        await session.refresh(policy, attribute_names=["rules"])
        decisions = {rule.tool: rule.decision for rule in policy.rules}
    finally:
        await generator.aclose()
    assert policy.description == MARKER
    assert decisions["notes.read"] == "allow"
    assert decisions["notes.write"] == "approval"


@pytest.mark.asyncio
async def test_startup_lists_agents_without_changing_them(client):
    from sqlalchemy import func, select

    generator, session = await _db_session()
    try:
        before = await session.scalar(select(func.count()).select_from(Agent))
        found = await log_agents_without_policy(session)
        after = await session.scalar(select(func.count()).select_from(Agent))
    finally:
        await generator.aclose()
    assert after == before
    assert isinstance(found, list)

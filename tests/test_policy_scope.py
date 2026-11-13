from sqlalchemy import select

from app.db.database import get_session
from app.main import app
from app.models import AuditLog
from tests.policy_helpers import activate_created


async def _policy(client, agent_ids):
    created = await client.post(
        "/policies",
        json={
            "name": "Refund Guard",
            "description": "Scoped to one agent",
            "agent_ids": agent_ids,
            "rules": [{"tool": "stripe.refund", "condition": "amount > 500", "decision": "allow"}],
        },
    )
    return created


async def test_policy_for_one_agent_is_paused_when_that_agent_is_removed(client):
    created = await _policy(client, ["agent_customer_ops"])
    activated = await activate_created(client, created)
    policy_id = activated["id"]
    version = activated["version"]

    removed = await client.delete("/agents/agent_customer_ops")
    assert removed.status_code == 204, removed.text

    after = await client.get(f"/policies/{policy_id}")
    assert after.status_code == 200, after.text
    body = after.json()
    # With no agents left it would apply to every agent, so it stops applying instead.
    assert body["agent_ids"] == []
    assert body["status"] == "paused"
    assert body["version"] == version + 1

    async for session in app.dependency_overrides[get_session]():
        events = list(await session.scalars(select(AuditLog).where(AuditLog.event == "policy_paused")))
        assert [item.payload["policy_id"] for item in events].count(policy_id) == 1


async def test_policy_shared_with_another_agent_stays_active(client):
    agents = [item["id"] for item in (await client.get("/agents")).json()]
    other = next(item for item in agents if item != "agent_customer_ops")
    created = await _policy(client, ["agent_customer_ops", other])
    policy_id = (await activate_created(client, created))["id"]

    assert (await client.delete("/agents/agent_customer_ops")).status_code == 204
    body = (await client.get(f"/policies/{policy_id}")).json()
    assert body["agent_ids"] == [other]
    assert body["status"] == "active"


async def test_unknown_agent_id_is_refused_and_keeps_the_scope(client):
    refused = await _policy(client, ["agent_customer_ops", "agent_missing"])
    assert refused.status_code == 400, refused.text

    created = await _policy(client, ["agent_customer_ops"])
    assert created.status_code == 201, created.text
    policy_id = created.json()["id"]
    changed = await client.patch(f"/policies/{policy_id}", json={"agent_ids": ["agent_missing"]})
    assert changed.status_code == 400, changed.text
    # The refused edit must not have cleared the list, which would mean "every agent".
    assert (await client.get(f"/policies/{policy_id}")).json()["agent_ids"] == ["agent_customer_ops"]

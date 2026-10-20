from tests.policy_helpers import activate_created


def test_simulator_amount_extraction():
    from app.features.execution.runs.services.simulator import extract_amount, extract_customer

    assert extract_amount("Refund Acme's duplicate charge.") == 840.0
    assert extract_amount("Refund 50 for Acme") == 50.0
    assert extract_amount("Refund 5000 for Acme") == 5000.0
    assert extract_customer("Refund Acme's duplicate charge.") == "Acme"


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_seeded_catalog(client):
    agents = (await client.get("/agents")).json()
    tools = (await client.get("/tools")).json()
    policies = (await client.get("/policies")).json()
    assert any(agent["id"] == "agent_customer_ops" for agent in agents)
    assert any(tool["name"] == "billing.refund" for tool in tools)
    refund = next(policy for policy in policies if policy["id"] == "policy_refund")
    assert [rule["decision"] for rule in refund["rules"]] == ["allow", "approval", "deny"]


async def test_full_execution_loop_requires_approval(client):
    created = await client.post(
        "/runs",
        json={
            "agent_id": "agent_customer_ops",
            "input": "Refund Acme's duplicate charge.",
        },
    )
    assert created.status_code == 201
    run = created.json()
    assert run["status"] == "waiting_approval"
    assert run["approval_id"]

    refund = next(action for action in run["actions"] if action["tool"] == "billing.refund")
    assert refund["status"] == "waiting_approval"
    assert refund["policy_decision"] == "approval_required"
    assert next(action for action in run["actions"] if action["tool"] == "crm.get_customer")[
        "status"
    ] == "completed"

    approved = await client.post(f"/approvals/{run['approval_id']}/approve")
    assert approved.status_code == 200
    body = approved.json()
    assert body == {
        "status": "completed",
        "action": "billing.refund",
        "result": "success",
    }

    finished = (await client.get(f"/runs/{run['id']}")).json()
    assert finished["status"] == "completed"

    activity = (await client.get("/activity", params={"run_id": run["id"]})).json()
    assert activity == []


async def test_small_refund_completes_without_approval(client):
    created = await client.post(
        "/runs",
        json={"agent_id": "agent_customer_ops", "input": "Refund 50 for Acme"},
    )
    run = created.json()
    assert created.status_code == 201
    assert run["status"] == "completed"
    assert run["approval_id"] is None
    refund = next(action for action in run["actions"] if action["tool"] == "billing.refund")
    assert refund["status"] == "completed"
    assert refund["policy_decision"] == "allow"


async def test_large_refund_is_denied(client):
    created = await client.post(
        "/runs",
        json={"agent_id": "agent_customer_ops", "input": "Refund 5000 for Acme"},
    )
    run = created.json()
    assert run["status"] == "failed"
    refund = next(action for action in run["actions"] if action["tool"] == "billing.refund")
    assert refund["status"] == "denied"
    assert refund["policy_decision"] == "deny"
    activity = (await client.get("/activity", params={"run_id": run["id"]})).json()
    assert activity == []


async def test_reject_cancels_run(client):
    created = await client.post(
        "/runs",
        json={"agent_id": "agent_customer_ops", "input": "Refund Acme's duplicate charge."},
    )
    approval_id = created.json()["approval_id"]
    rejected = await client.post(f"/approvals/{approval_id}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    run = (await client.get(f"/runs/{created.json()['id']}")).json()
    assert run["status"] == "cancelled"


async def test_approve_rechecks_policy(client):
    created = await client.post(
        "/runs",
        json={"agent_id": "agent_customer_ops", "input": "Refund Acme's duplicate charge."},
    )
    approval_id = created.json()["approval_id"]
    tightened = await client.post(
        "/policies",
        json={
            "name": "Block refunds",
            "rules": [
                {"tool": "billing.refund", "condition": "amount > 0", "decision": "deny"},
            ],
        },
    )
    await activate_created(client, tightened)
    response = await client.post(f"/approvals/{approval_id}/approve")
    assert response.status_code == 403

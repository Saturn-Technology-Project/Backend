from decimal import Decimal

from app.features.billing.x402.services.x402 import payment_requirements, settle_payment, verify_payment


async def test_agent_wallet_is_seeded(client):
    wallet = (await client.get("/agents/agent_customer_ops/wallet")).json()
    assert wallet["id"] == "wallet_customer_ops"
    assert wallet["asset"] == "USDC"
    assert Decimal(wallet["balance"]) == Decimal("100")
    assert wallet["network"] == "base-sepolia"


async def test_refund_settles_x402_from_agent_wallet(client):
    created = await client.post(
        "/runs",
        json={"agent_id": "agent_customer_ops", "input": "Refund 50 for Acme"},
    )
    run = created.json()
    assert run["status"] == "completed"
    refund = next(action for action in run["actions"] if action["tool"] == "billing.refund")
    assert refund["result"]["x402"]["tx_hash"].startswith("0x")

    payments = (await client.get("/payments", params={"run_id": run["id"]})).json()
    assert len(payments) == 1
    assert payments[0]["status"] == "settled"
    assert Decimal(payments[0]["amount"]) == Decimal("0.05")

    wallet = (await client.get("/wallets/wallet_customer_ops")).json()
    assert Decimal(wallet["balance"]) == Decimal("99.95")
    assert Decimal(wallet["spent_today"]) == Decimal("0.05")

    activity = (await client.get("/activity", params={"run_id": run["id"]})).json()
    assert activity == []


async def test_empty_wallet_blocks_paid_tool(client):
    created_agent = await client.post(
        "/agents",
        json={"name": "Broke Ops", "description": "No funds", "model": "claude-sonnet"},
    )
    agent_id = created_agent.json()["id"]
    wallet = (await client.get(f"/agents/{agent_id}/wallet")).json()
    assert Decimal(wallet["balance"]) == Decimal("0")

    created = await client.post(
        "/runs",
        json={"agent_id": agent_id, "input": "Refund 50 for Acme"},
    )
    run = created.json()
    assert run["status"] == "failed"
    refund = next(action for action in run["actions"] if action["tool"] == "billing.refund")
    assert refund["status"] == "failed"
    assert refund["result"]["reason"] == "insufficient_balance"

    activity = (await client.get("/activity", params={"run_id": run["id"]})).json()
    assert activity == []


async def test_x402_facilitator_supported(client):
    body = (await client.get("/x402/supported")).json()
    assert body["kinds"][0]["scheme"] == "exact"
    assert body["kinds"][0]["network"] == "base-sepolia"


def test_x402_verify_and_settle_shapes():
    requirements = {
        "x402Version": 1,
        "accepts": [
            {
                "scheme": "exact",
                "network": "base-sepolia",
                "maxAmountRequired": "50000",
                "payTo": "0x00000000000000000000000000000000000C0DE",
                "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            }
        ],
    }
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "base-sepolia",
        "payload": {
            "signature": "0xabc",
            "authorization": {
                "from": "0xa11ce",
                "to": "0x00000000000000000000000000000000000C0DE",
                "value": "50000",
            },
        },
    }
    assert verify_payment(payload, requirements)["isValid"] is True
    settled = settle_payment(payload, requirements)
    assert settled["success"] is True
    assert settled["transaction"].startswith("0x")

    bad = dict(payload)
    bad["payload"] = {"signature": "", "authorization": payload["payload"]["authorization"]}
    assert verify_payment(bad, requirements)["isValid"] is False


def test_paid_tool_emits_payment_required():
    class Tool:
        name = "billing.refund"
        description = "Refund a customer payment"
        mcp_endpoint = "mcp://billing/refund"
        x402 = {"price": "0.05", "network": "base-sepolia"}

    requirements = payment_requirements(Tool())
    assert requirements["accepts"][0]["maxAmountRequired"] == "50000"

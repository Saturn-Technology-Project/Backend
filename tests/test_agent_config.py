from app.services.agent_config import assigned_tool_names, public_settings, system_prompt
from app.models.agent import Agent
from tests.test_ws import _login, ws_client


def test_assigned_tools_and_prompt_defaults():
    agent = Agent(
        id="agent_x",
        organization_id="org_x",
        name="Billing",
        description="Fallback prompt",
        model="claude-sonnet",
        settings={},
    )
    catalog = ["billing.get_invoice", "billing.refund", "crm.get_customer"]
    assert assigned_tool_names(agent.settings, catalog) == catalog
    assert system_prompt(agent) == "Fallback prompt"
    assert public_settings(agent)["tools"] is None

    agent.settings = {
        "instructions": "Verify the invoice first.",
        "tools": ["billing.refund", "missing.tool"],
    }
    assert assigned_tool_names(agent.settings, catalog) == ["billing.refund"]
    assert system_prompt(agent) == "Verify the invoice first."
    assert public_settings(agent)["tools"] == ["billing.refund", "missing.tool"]


def test_patch_agent_settings_persists_and_pushes_to_connector():
    with ws_client() as client:
        _login(client)
        updated = client.patch(
            "/agents/agent_customer_ops",
            json={
                "settings": {
                    "instructions": "Investigate billing issues. Be concise.",
                    "response_style": "concise",
                    "autonomy": "suggest_only",
                    "tools": ["billing.get_payment", "billing.refund"],
                    "provider": "openai",
                    "model": "gpt-5.6",
                    "temperature": 0.1,
                    "max_output": 2000,
                }
            },
        )
        assert updated.status_code == 200, updated.text
        body = updated.json()
        assert body["model"] == "gpt-5.6"
        assert body["settings"]["instructions"].startswith("Investigate billing")
        assert body["settings"]["response_style"] == "concise"
        assert body["settings"]["autonomy"] == "suggest_only"
        assert body["settings"]["tools"] == ["billing.get_payment", "billing.refund"]
        assert body["pushed"] is False

        enroll = client.post("/connectors/enroll").json()
        with client.websocket_connect(
            f"/ws/connectors?connector_id={enroll['connector_id']}&token={enroll['token']}"
        ) as connector_ws:
            assert connector_ws.receive_json()["type"] == "connection.ack"
            assert connector_ws.receive_json()["type"] == "connector.registered"
            connector_ws.send_json(
                {
                    "type": "runtime.register",
                    "runtime_id": "runtime_finance",
                    "name": "Finance Runtime",
                    "agents": ["agent_customer_ops"],
                }
            )
            assert connector_ws.receive_json()["type"] == "runtime.registered"
            replayed = connector_ws.receive_json()
            assert replayed["type"] == "command"
            assert replayed["command"] == "config.update"
            assert replayed["payload"]["model"] == "gpt-5.6"

            live = client.patch(
                "/agents/agent_customer_ops",
                json={
                    "runtime_id": "runtime_ops",
                    "daily_spend_limit": "50",
                    "per_tx_limit": "5",
                    "settings": {
                        "environment": "ACME Production",
                        "region": "Taiwan",
                        "max_concurrent_runs": 3,
                    },
                },
            )
            assert live.status_code == 200, live.text
            assert live.json()["pushed"] is True
            assert live.json()["runtime_id"] == "runtime_ops"
            assert live.json()["settings"]["region"] == "Taiwan"

            command = connector_ws.receive_json()
            assert command["type"] == "command"
            assert command["command"] == "config.update"
            assert command["agent_id"] == "agent_customer_ops"
            payload = command["payload"]
            assert payload["instructions"].startswith("Investigate billing")
            assert payload["tools"] == ["billing.get_payment", "billing.refund"]
            assert payload["runtime_id"] == "runtime_ops"
            assert float(payload["wallet"]["daily_spend"]) == 50
            assert float(payload["wallet"]["per_tx"]) == 5
            assert payload["max_concurrent_runs"] == 3

        wallet = client.get("/agents/agent_customer_ops/wallet").json()
        assert float(wallet["daily_spend_limit"]) == 50
        assert float(wallet["per_tx_limit"]) == 5

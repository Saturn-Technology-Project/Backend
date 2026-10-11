from app.features.agent.agents.services.agent_config import (
    assigned_tool_names,
    default_instructions,
    infer_provider,
    public_settings,
    system_prompt,
)
from app.features.agent.agents.models.agent import Agent


def test_infer_provider_for_known_and_custom_models():
    assert infer_provider("gpt-4o") == "openai"
    assert infer_provider("gemini-2.5-pro") == "google"
    assert infer_provider("deepseek-chat") == "deepseek"
    assert infer_provider("claude-sonnet") == "anthropic"
    assert infer_provider("llama-3.1-70b") == "custom"


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
    prompt = system_prompt(agent)
    assert prompt == default_instructions("Billing", "Fallback prompt")
    assert prompt.startswith("You are Billing.")
    assert "Fallback prompt" in prompt
    assert public_settings(agent)["instructions"] == prompt
    assert public_settings(agent)["tools"] is None
    assert public_settings(agent)["provider"] == "anthropic"
    assert public_settings(agent)["base_url"] == "https://api.anthropic.com"

    agent.settings = {
        "instructions": "Verify the invoice first.",
        "tools": ["billing.refund", "missing.tool"],
    }
    assert assigned_tool_names(agent.settings, catalog) == ["billing.refund"]
    assert system_prompt(agent) == "Verify the invoice first."
    assert public_settings(agent)["tools"] == ["billing.refund", "missing.tool"]

    agent.model = "llama-3.1-70b"
    agent.settings = {"provider": "custom", "model": "llama-3.1-70b", "base_url": "https://llm.example.com/v1"}
    settings = public_settings(agent)
    assert settings["provider"] == "custom"
    assert settings["base_url"] == "https://llm.example.com/v1"


async def test_patch_agent_settings_persists(client):
    updated = await client.patch(
        "/agents/agent_customer_ops",
        json={
            "settings": {
                "instructions": "Investigate billing issues. Be concise.",
                "response_style": "concise",
                "autonomy": "suggest_only",
                "tools": ["billing.get_payment", "billing.refund"],
                "provider": "deepseek",
                "model": "deepseek-chat",
                "base_url": "",
                "temperature": 0.1,
                "max_output": 2000,
            }
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["model"] == "deepseek-chat"
    assert body["settings"]["provider"] == "deepseek"
    assert body["settings"]["instructions"].startswith("Investigate billing")
    assert body["settings"]["response_style"] == "concise"
    assert body["settings"]["autonomy"] == "suggest_only"
    assert body["settings"]["tools"] == ["billing.get_payment", "billing.refund"]
    assert body["pushed"] is False
    activity = (await client.get("/activity")).json()
    assert [item for item in activity if item["event"] == "prompt_updated"] == []

    live = await client.patch(
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
    assert live.json()["pushed"] is False
    assert live.json()["runtime_id"] == "runtime_ops"
    assert live.json()["settings"]["region"] == "Taiwan"

    wallet = (await client.get("/agents/agent_customer_ops/wallet")).json()
    assert float(wallet["daily_spend_limit"]) == 50
    assert float(wallet["per_tx_limit"]) == 5

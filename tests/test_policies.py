async def test_create_policy_with_structured_rules(client):
    created = await client.post(
        "/policies",
        json={
            "name": "Refund Policy",
            "description": "Control customer refunds",
            "environment": "production",
            "agent_ids": ["agent_customer_ops"],
            "tool": "billing.refund",
            "rules": [
                {
                    "conditions": [{"field": "amount", "operator": "lte", "value": 100}],
                    "decision": "ALLOW",
                },
                {
                    "conditions": [
                        {"field": "amount", "operator": "gt", "value": 100},
                        {"field": "amount", "operator": "lte", "value": 1000},
                    ],
                    "decision": "APPROVAL_REQUIRED",
                },
                {
                    "conditions": [{"field": "amount", "operator": "gt", "value": 1000}],
                    "decision": "DENY",
                },
            ],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["version"] == 1
    # Created as a draft even though nothing asked otherwise; activation goes through validate/activate.
    assert body["status"] == "draft"
    assert body["agent_ids"] == ["agent_customer_ops"]
    assert [rule["decision"] for rule in body["rules"]] == [
        "allow",
        "approval_required",
        "deny",
    ]
    assert body["rules"][1]["conditions"][0]["operator"] == "gt"

    patched = await client.patch(
        f"/policies/{body['id']}",
        json={"description": "Updated refund control"},
    )
    assert patched.status_code == 200
    assert patched.json()["version"] == 2
    assert patched.json()["description"] == "Updated refund control"

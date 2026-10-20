async def activate_created(client, created, headers=None) -> dict:
    """Policies are created as drafts. Validate one and activate it, confirming its allows."""
    assert created.status_code == 201, created.text
    policy_id = created.json()["id"]
    assert created.json()["status"] == "draft"
    validated = await client.post(f"/policies/{policy_id}/validate", headers=headers)
    assert validated.status_code == 200, validated.text
    assert validated.json()["report"]["ok"], validated.text
    activated = await client.post(f"/policies/{policy_id}/activate", headers=headers, json={"confirm": True})
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "active"
    return activated.json()


async def create_active_policy(client, body: dict, headers=None) -> dict:
    created = await client.post("/policies", headers=headers, json=body)
    return await activate_created(client, created, headers)

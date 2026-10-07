async def test_subscription_has_no_synthetic_billing(client):
    response = await client.get("/org/subscription")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["organization_id"] == "org_saturn"
    assert body["plan"]["kind"] == "plan"
    assert body["plan"]["id"] in {"free", "pro", "enterprise"}
    assert body["payment"]["demo"] is False
    assert body["payment"]["last4"] == ""
    assert body["invoices"] == []

    missing = await client.post("/org/subscription/invoices/inv_missing/pay")
    assert missing.status_code in {400, 404}

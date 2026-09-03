async def test_subscription_shows_plan_and_demo_pay(client):
    response = await client.get("/org/subscription")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["organization_id"] == "org_saturn"
    assert body["plan"]["kind"] == "plan"
    assert body["plan"]["id"] in {"trial", "starter", "enterprise"}
    assert body["payment"]["demo"] is True
    assert body["payment"]["last4"] == "4242"
    assert len(body["invoices"]) >= 1

    opened = next((item for item in body["invoices"] if item["status"] == "open"), None)
    assert opened is not None
    paid = await client.post(f"/org/subscription/invoices/{opened['id']}/pay")
    assert paid.status_code == 200, paid.text
    updated = next(item for item in paid.json()["invoices"] if item["id"] == opened["id"])
    assert updated["status"] == "paid"
    assert updated["paid_at"]

    again = await client.post(f"/org/subscription/invoices/{opened['id']}/pay")
    assert again.status_code == 409

    missing = await client.post("/org/subscription/invoices/inv_missing/pay")
    assert missing.status_code == 404

async def test_me_includes_enterprise_employee_agents_and_mcp(client):
    me = (await client.get("/auth/me")).json()
    assert me["id"] == "user_operator"
    assert me["profile_id"] == "org_saturn:user_operator"
    assert me["organization"]["id"] == "org_saturn"
    assert me["enterprise"]["id"] == "org_saturn"
    assert me["enterprise"]["name"] == me["organization"]["name"]
    assert me["employee"]["id"] == "emp_operator"
    assert me["employee"]["title"] == "Operator"
    assert {agent["id"] for agent in me["agents"]} >= {"agent_customer_ops", "agent_unmapped"}
    assert "agent_customer_ops" in me["assigned_agent_ids"]
    assert "agent_unmapped" not in me["assigned_agent_ids"]
    names = {server["name"] for server in me["mcp_servers"]}
    assert names == {"Billing", "CRM", "Email"}
    billing = next(server for server in me["mcp_servers"] if server["id"] == "mcp_billing")
    assert "billing.refund" in billing["tools"]
    assert billing["connected"] is True
    assert me["mcp_gateway"]["status"] == "connected"
    assert me["mcp_gateway"]["connected"] == 3
    assert me["mcp_gateway"]["total"] == 3
    assert any(user["email"] == "rick@saturn.ai" for user in me["users"])


async def test_catalog_is_scoped_to_org(client):
    agents = (await client.get("/agents")).json()
    assert {agent["id"] for agent in agents} >= {"agent_customer_ops", "agent_unmapped"}
    assert (await client.get("/agents/agent_unmapped")).status_code == 200

    tools = (await client.get("/tools")).json()
    assert {tool["name"] for tool in tools} == {
        "billing.get_payment",
        "billing.refund",
        "crm.get_customer",
        "email.send",
    }
    refund = next(tool for tool in tools if tool["name"] == "billing.refund")
    assert refund["mcp_server_id"] == "mcp_billing"

    servers = (await client.get("/mcp-servers")).json()
    assert {server["id"] for server in servers} == {"mcp_billing", "mcp_crm", "mcp_email"}
    assert all(server["connected"] is True for server in servers)

    org = (await client.get("/org")).json()
    assert {agent["id"] for agent in org["agents"]} >= {"agent_customer_ops"}
    assert {server["id"] for server in org["mcp_servers"]} == {"mcp_billing", "mcp_crm", "mcp_email"}
    assert any(user["email"] == "rick@saturn.ai" for user in org["users"])
    users = (await client.get("/org/users")).json()
    assert any(user["email"] == "rick@saturn.ai" for user in users)


async def test_unmapped_agent_cannot_run(client):
    created = await client.post(
        "/runs",
        json={"agent_id": "agent_unmapped", "input": "Refund 50 for Acme"},
    )
    assert created.status_code == 404


async def test_creating_agent_assigns_it_to_employee(client):
    created = await client.post(
        "/agents",
        json={"name": "Broke Ops", "description": "No funds", "model": "claude-sonnet"},
    )
    assert created.status_code == 201
    agent_id = created.json()["id"]

    agents = (await client.get("/agents")).json()
    assert any(agent["id"] == agent_id for agent in agents)

    me = (await client.get("/auth/me")).json()
    assert any(agent["id"] == agent_id for agent in me["agents"])
    assert agent_id in me["assigned_agent_ids"]

    wallet = await client.get(f"/agents/{agent_id}/wallet")
    assert wallet.status_code == 200

"""Test-only catalog. Production startup does not load this."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.ids import new_id
from app.db.seed import seed_if_empty
from app.models import Agent, McpServer, Policy, PolicyRule, Tool, User

settings = get_settings()

TOOLS = [
    {
        "id": "tool_crm_get_customer",
        "name": "crm.get_customer",
        "description": "Look up a customer record",
        "mcp_endpoint": "mcp://crm/get_customer",
        "permissions": ["read"],
    },
    {
        "id": "tool_billing_get_payment",
        "name": "billing.get_payment",
        "description": "Fetch a customer payment",
        "mcp_endpoint": "mcp://billing/get_payment",
        "permissions": ["read"],
    },
    {
        "id": "tool_billing_refund",
        "name": "billing.refund",
        "description": "Refund a customer payment",
        "mcp_endpoint": "mcp://billing/refund",
        "permissions": ["write"],
    },
    {
        "id": "tool_email_send",
        "name": "email.send",
        "description": "Send an email",
        "mcp_endpoint": "mcp://email/send",
        "permissions": ["write"],
    },
]

MCP_SERVERS = [
    {"id": "mcp_crm", "name": "CRM", "endpoint": "mcp://crm"},
    {"id": "mcp_billing", "name": "Billing", "endpoint": "mcp://billing"},
    {"id": "mcp_email", "name": "Email", "endpoint": "mcp://email"},
]

TOOL_SERVERS = {
    "tool_crm_get_customer": "mcp_crm",
    "tool_billing_get_payment": "mcp_billing",
    "tool_billing_refund": "mcp_billing",
    "tool_email_send": "mcp_email",
}

REFUND_RULES = [
    (
        "billing.refund",
        "amount <= 100",
        [{"field": "amount", "operator": "lte", "value": 100}],
        "allow",
    ),
    (
        "billing.refund",
        "amount > 100 AND amount <= 1000",
        [
            {"field": "amount", "operator": "gt", "value": 100},
            {"field": "amount", "operator": "lte", "value": 1000},
        ],
        "approval",
    ),
    (
        "billing.refund",
        "amount > 1000",
        [{"field": "amount", "operator": "gt", "value": 1000}],
        "deny",
    ),
]


async def seed_test_catalog(session: AsyncSession) -> None:
    await seed_if_empty(session)

    if await session.get(Agent, "agent_customer_ops") is None:
        session.add(
            Agent(
                id="agent_customer_ops",
                organization_id=settings.default_org_id,
                name="Customer Operations",
                description="Handles customer support and billing operations",
                model="claude-sonnet",
            )
        )

    if await session.get(Agent, "agent_unmapped") is None:
        session.add(
            Agent(
                id="agent_unmapped",
                organization_id=settings.default_org_id,
                name="Unmapped Agent",
                description="Present in the enterprise but not assigned to the seed employee",
                model="claude-sonnet",
            )
        )

    for spec in MCP_SERVERS:
        if await session.get(McpServer, spec["id"]) is None:
            session.add(
                McpServer(
                    id=spec["id"],
                    organization_id=settings.default_org_id,
                    name=spec["name"],
                    endpoint=spec["endpoint"],
                    status="active",
                )
            )
    await session.flush()

    for tool in TOOLS:
        found = await session.get(Tool, tool["id"])
        if found is None:
            session.add(
                Tool(
                    organization_id=settings.default_org_id,
                    type="mcp",
                    status="active",
                    mcp_server_id=TOOL_SERVERS.get(tool["id"]),
                    **tool,
                )
            )
        elif found.mcp_server_id is None:
            found.mcp_server_id = TOOL_SERVERS.get(tool["id"])

    result = await session.execute(
        select(Policy)
        .options(selectinload(Policy.agents))
        .where(Policy.id == "policy_refund")
    )
    policy = result.scalar_one_or_none()
    if policy is None:
        policy = Policy(
            id="policy_refund",
            organization_id=settings.default_org_id,
            name="Refund Policy",
            description="Control customer refunds",
            status="active",
            version=1,
            environment="production",
        )
        session.add(policy)
        await session.flush()
        result = await session.execute(
            select(Policy)
            .options(selectinload(Policy.agents))
            .where(Policy.id == "policy_refund")
        )
        policy = result.scalar_one()
    else:
        if not policy.description:
            policy.description = "Control customer refunds"
        policy.status = policy.status or "active"
        policy.version = policy.version or 1
        policy.environment = policy.environment or "production"

    billing = await session.get(Agent, "agent_customer_ops")
    if billing is not None and all(agent.id != billing.id for agent in policy.agents):
        policy.agents.append(billing)

    result = await session.execute(
        select(PolicyRule).where(PolicyRule.policy_id == "policy_refund")
    )
    if result.first() is None:
        for position, (tool, condition, conditions, decision) in enumerate(REFUND_RULES):
            session.add(
                PolicyRule(
                    id=new_id("rule"),
                    policy_id="policy_refund",
                    tool=tool,
                    condition=condition,
                    conditions=conditions,
                    decision=decision,
                    position=position,
                )
            )

    await session.flush()
    user = await session.get(User, settings.default_actor_id)
    if user is None:
        return
    from app.features.organization.org.services.mapping_service import mapping_service

    employee = await mapping_service.employee_for_user(session, user.id)
    if employee is None:
        return
    result = await session.execute(
        select(Agent)
        .options(selectinload(Agent.mcp_servers))
        .where(Agent.id == "agent_customer_ops")
    )
    mapped = result.scalar_one_or_none()
    servers = [
        server
        for spec in MCP_SERVERS
        if (server := await session.get(McpServer, spec["id"])) is not None
    ]
    if mapped is not None:
        if mapped not in employee.agents:
            employee.agents.append(mapped)
        for server in servers:
            if server not in mapped.mcp_servers:
                mapped.mcp_servers.append(server)
    for server in servers:
        if server not in employee.mcp_servers:
            employee.mcp_servers.append(server)
    await session.flush()

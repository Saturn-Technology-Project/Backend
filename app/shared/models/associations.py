from sqlalchemy import Column, ForeignKey, String, Table

from app.db.database import Base

employee_agents = Table(
    "employee_agents",
    Base.metadata,
    Column("employee_id", String(64), ForeignKey("employees.id"), primary_key=True),
    Column("agent_id", String(64), ForeignKey("agents.id"), primary_key=True),
)

employee_mcp_servers = Table(
    "employee_mcp_servers",
    Base.metadata,
    Column("employee_id", String(64), ForeignKey("employees.id"), primary_key=True),
    Column("mcp_server_id", String(64), ForeignKey("mcp_servers.id"), primary_key=True),
)

agent_mcp_servers = Table(
    "agent_mcp_servers",
    Base.metadata,
    Column("agent_id", String(64), ForeignKey("agents.id"), primary_key=True),
    Column("mcp_server_id", String(64), ForeignKey("mcp_servers.id"), primary_key=True),
)

agent_policies = Table(
    "agent_policies",
    Base.metadata,
    Column("agent_id", String(64), ForeignKey("agents.id"), primary_key=True),
    Column("policy_id", String(64), ForeignKey("policies.id"), primary_key=True),
)

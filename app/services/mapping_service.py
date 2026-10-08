from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.models import Agent, Employee, McpServer, Tool, User
from app.services.access import allows, permissions_for_role
from app.services.mcp_gateway import mcp_gateway

settings = get_settings()


class MappingService:
    async def ensure_bootstrap_employee(self, session: AsyncSession, user: User) -> Employee | None:
        employee = await self.employee_for_user(session, user.id)
        if employee is not None:
            return employee
        if user.id != settings.default_actor_id:
            return None
        suffix = user.id[5:] if user.id.startswith("user_") else user.id
        employee = Employee(
            id="emp_" + suffix,
            organization_id=user.organization_id,
            user_id=user.id,
            title="Operator",
            role="admin",
            permissions=permissions_for_role("admin"),
            status="active",
        )
        session.add(employee)
        await session.flush()
        employee = await self.employee_for_user(session, user.id)
        if employee is None:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to map employee")
        return employee

    async def bind_workspace(self, session: AsyncSession, user: User) -> Employee | None:
        employee = await self.employee_for_user(session, user.id)
        if employee is None:
            employee = await self.ensure_bootstrap_employee(session, user)
        if employee is None or employee.status != "active":
            return employee

        await mcp_gateway.connect_organization(session, user.organization_id)
        org_servers = await self.list_org_mcp_servers(session, user.organization_id)
        if allows(employee.permissions, "agents", "read") and not employee.mcp_servers:
            employee.mcp_servers.extend(org_servers)
            await session.flush()
            employee = await self.employee_for_user(session, user.id)
        return employee

    async def list_org_agents(self, session: AsyncSession, organization_id: str) -> list[Agent]:
        result = await session.execute(
            select(Agent)
            .options(selectinload(Agent.mcp_servers))
            .where(Agent.organization_id == organization_id)
            .order_by(Agent.created_at.asc())
        )
        return list(result.scalars().unique().all())

    async def list_org_mcp_servers(
        self, session: AsyncSession, organization_id: str
    ) -> list[McpServer]:
        result = await session.execute(
            select(McpServer)
            .options(selectinload(McpServer.tools))
            .where(McpServer.organization_id == organization_id)
            .order_by(McpServer.name.asc())
        )
        return list(result.scalars().unique().all())

    async def list_org_tools(self, session: AsyncSession, organization_id: str) -> list[Tool]:
        result = await session.execute(
            select(Tool)
            .where(Tool.organization_id == organization_id)
            .order_by(Tool.name.asc())
        )
        return list(result.scalars().all())

    async def require_org_agent(self, session: AsyncSession, user: User, agent_id: str) -> Agent:
        agent = await session.get(Agent, agent_id)
        if agent is None or agent.organization_id != user.organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
        return agent

    async def employee_for_user(self, session: AsyncSession, user_id: str) -> Employee | None:
        result = await session.execute(
            select(Employee)
            .options(
                selectinload(Employee.user),
                selectinload(Employee.agents),
                selectinload(Employee.mcp_servers).selectinload(McpServer.tools),
                selectinload(Employee.organization),
            )
            .where(Employee.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def require_employee(self, session: AsyncSession, user: User) -> Employee:
        employee = await self.employee_for_user(session, user.id)
        if employee is None or employee.status != "active":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No employee mapping for this user")
        return employee

    async def agent_ids_for(self, session: AsyncSession, user: User) -> set[str]:
        employee = await self.employee_for_user(session, user.id)
        if employee is None:
            return set()
        return {agent.id for agent in employee.agents}

    async def mcp_server_ids_for(self, session: AsyncSession, user: User) -> set[str]:
        employee = await self.employee_for_user(session, user.id)
        if employee is None:
            return set()
        return {server.id for server in employee.mcp_servers}

    async def require_agent(self, session: AsyncSession, user: User, agent_id: str) -> Agent:
        ids = await self.agent_ids_for(session, user)
        if agent_id not in ids:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
        agent = await session.get(Agent, agent_id)
        if agent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
        return agent

    async def assign_agent(self, session: AsyncSession, user: User, agent: Agent) -> None:
        employee = await self.require_employee(session, user)
        result = await session.execute(
            select(Agent)
            .options(selectinload(Agent.mcp_servers))
            .where(Agent.id == agent.id)
        )
        mapped = result.scalar_one()
        if mapped not in employee.agents:
            employee.agents.append(mapped)
        for server in employee.mcp_servers:
            if server not in mapped.mcp_servers:
                mapped.mcp_servers.append(server)
        await session.flush()

    async def list_mcp_servers(self, session: AsyncSession, user: User) -> list[McpServer]:
        servers = await self.list_org_mcp_servers(session, user.organization_id)
        return servers

    async def _org_employees(self, session: AsyncSession, organization_id: str) -> list[Employee]:
        result = await session.execute(
            select(Employee)
            .options(
                selectinload(Employee.agents),
                selectinload(Employee.mcp_servers),
            )
            .where(
                Employee.organization_id == organization_id,
                Employee.status == "active",
            )
        )
        return list(result.scalars().unique().all())

    async def share_org_agents(
        self, session: AsyncSession, organization_id: str, agents: list[Agent]
    ) -> None:
        if not agents:
            return
        servers = await self.list_org_mcp_servers(session, organization_id)
        for employee in await self._org_employees(session, organization_id):
            if not allows(employee.permissions, "agents", "read"):
                continue
            for agent in agents:
                if agent not in employee.agents:
                    employee.agents.append(agent)
                for server in servers:
                    if server not in agent.mcp_servers:
                        agent.mcp_servers.append(server)
        await session.flush()

    async def share_org_mcp(
        self, session: AsyncSession, organization_id: str, servers: list[McpServer]
    ) -> None:
        if not servers:
            return
        agents = await self.list_org_agents(session, organization_id)
        for employee in await self._org_employees(session, organization_id):
            if not allows(employee.permissions, "agents", "read"):
                continue
            for server in servers:
                if server not in employee.mcp_servers:
                    employee.mcp_servers.append(server)
                for agent in agents:
                    if server not in agent.mcp_servers:
                        agent.mcp_servers.append(server)
        await session.flush()


mapping_service = MappingService()

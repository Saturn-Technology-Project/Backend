from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Action,
    Agent,
    Approval,
    AuditLog,
    Employee,
    Invitation,
    McpServer,
    Run,
    Tool,
    ToolSetItem,
    User,
)
from app.shared.models.associations import (
    agent_mcp_servers,
    agent_policies,
    employee_agents,
    employee_mcp_servers,
)
from app.features.organization.org.services.mapping_service import mapping_service


class RemovalService:
    async def delete_agent(self, session: AsyncSession, user: User, agent_id: str) -> None:
        agent = await mapping_service.require_org_agent(session, user, agent_id)
        run_ids = list(
            (await session.execute(select(Run.id).where(Run.agent_id == agent.id))).scalars()
        )
        action_ids = []
        if run_ids:
            action_ids = list(
                (await session.execute(select(Action.id).where(Action.run_id.in_(run_ids)))).scalars()
            )
        if action_ids:
            await session.execute(
                update(AuditLog).where(AuditLog.action_id.in_(action_ids)).values(action_id=None)
            )
            await session.execute(delete(Approval).where(Approval.action_id.in_(action_ids)))
        if run_ids:
            await session.execute(
                update(AuditLog).where(AuditLog.run_id.in_(run_ids)).values(run_id=None)
            )
            await session.execute(delete(Action).where(Action.run_id.in_(run_ids)))
            await session.execute(delete(Run).where(Run.id.in_(run_ids)))
        await session.execute(delete(employee_agents).where(employee_agents.c.agent_id == agent.id))
        await session.execute(delete(agent_mcp_servers).where(agent_mcp_servers.c.agent_id == agent.id))
        await session.execute(delete(agent_policies).where(agent_policies.c.agent_id == agent.id))
        await self._drop_json_id(session, agent.organization_id, "agent", agent.id)
        self._unlink_loaded(session, agent, ("agents",))
        session.expire(agent, ["runs", "employees", "mcp_servers", "policies"])
        for obj in list(session.identity_map.values()):
            if isinstance(obj, Employee) and "agents" in obj.__dict__:
                session.expire(obj, ["agents"])
        await session.delete(agent)
        await session.flush()

    async def delete_mcp_server(self, session: AsyncSession, user: User, server_id: str) -> None:
        server = await session.get(McpServer, server_id)
        if server is None or server.organization_id != user.organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "MCP server not found")
        await session.execute(
            delete(employee_mcp_servers).where(employee_mcp_servers.c.mcp_server_id == server.id)
        )
        await session.execute(
            delete(agent_mcp_servers).where(agent_mcp_servers.c.mcp_server_id == server.id)
        )
        tool_ids = select(Tool.id).where(Tool.mcp_server_id == server.id)
        await session.execute(delete(ToolSetItem).where(ToolSetItem.tool_id.in_(tool_ids)))
        await session.execute(delete(Tool).where(Tool.mcp_server_id == server.id))
        for obj in list(session.identity_map.values()):
            if isinstance(obj, Tool) and obj.mcp_server_id == server.id:
                session.expunge(obj)
        await self._drop_json_id(session, server.organization_id, "mcp", server.id)
        self._unlink_loaded(session, server, ("mcp_servers",))
        session.expire(server, ["tools", "employees", "agents"])
        for obj in list(session.identity_map.values()):
            if isinstance(obj, Employee) and "mcp_servers" in obj.__dict__:
                session.expire(obj, ["mcp_servers"])
            if isinstance(obj, Agent) and "mcp_servers" in obj.__dict__:
                session.expire(obj, ["mcp_servers"])
        from app.features.credentials.vault.services.secret_vault import delete_secret

        await delete_secret(session, server.credential_id)
        await session.delete(server)
        await session.flush()

    async def _drop_json_id(
        self, session: AsyncSession, organization_id: str, kind: str, item_id: str
    ) -> None:
        employees = (
            await session.execute(select(Employee).where(Employee.organization_id == organization_id))
        ).scalars()
        for employee in employees:
            if kind == "agent" and employee.agent_edit_ids and item_id in employee.agent_edit_ids:
                employee.agent_edit_ids = [item for item in employee.agent_edit_ids if item != item_id]
        invites = (
            await session.execute(select(Invitation).where(Invitation.organization_id == organization_id))
        ).scalars()
        for invite in invites:
            if kind == "agent" and item_id in (invite.agent_ids or []):
                invite.agent_ids = [item for item in invite.agent_ids if item != item_id]
            if kind == "mcp" and item_id in (invite.mcp_server_ids or []):
                invite.mcp_server_ids = [item for item in invite.mcp_server_ids if item != item_id]


    def _unlink_loaded(self, session: AsyncSession, target: object, attributes: tuple[str, ...]) -> None:
        for obj in list(session.identity_map.values()):
            if obj is target:
                continue
            for name in attributes:
                loaded = obj.__dict__.get(name)
                if loaded is None or target not in loaded:
                    continue
                loaded.remove(target)


removal_service = RemovalService()

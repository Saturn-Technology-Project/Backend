from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ids import new_id
from app.models import Action, Agent, Approval, Run, Tool, ToolSet, ToolSetItem, User
from app.models.enums import ActionStatus, ActorType, ApprovalStatus, AuditEvent, PolicyDecision, RunStatus
from app.services.audit_service import audit_service
from app.services.credential_service import open_secret
from app.services.mcp_remote import call_remote_tool
from app.services.policy_engine import policy_engine


def rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: Any, code: int, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


class SaturnMcp:
    async def handle(
        self,
        session: AsyncSession,
        user: User,
        *,
        agent_id: str,
        run_id: str,
        action_id: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = message.get("id", 1)
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            return rpc_result(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "Saturn MCP", "version": "0.1.0"},
                },
            )
        if method in {"notifications/initialized", "ping"}:
            return rpc_result(request_id, {})
        agent = await session.get(Agent, agent_id) if agent_id else None
        if agent is None or agent.organization_id != user.organization_id:
            return rpc_error(request_id, -32000, "Unknown agent")
        if method == "tools/list":
            exposed = await self._exposed(session, agent)
            return rpc_result(
                request_id,
                {
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description or tool.name,
                            "inputSchema": tool.input_schema or {"type": "object", "properties": {}},
                        }
                        for tool, _item in exposed
                    ]
                },
            )
        if method == "tools/call":
            return await self._call(
                session,
                user,
                agent,
                request_id,
                name=str(params.get("name") or ""),
                arguments=params.get("arguments") if isinstance(params.get("arguments"), dict) else {},
                run_id=run_id,
                action_id=action_id,
            )
        return rpc_error(request_id, -32601, "Method not found")

    async def _exposed(self, session: AsyncSession, agent: Agent) -> list[tuple[Tool, ToolSetItem]]:
        if not agent.tool_set_id:
            return []
        result = await session.execute(
            select(ToolSetItem)
            .options(selectinload(ToolSetItem.tool).selectinload(Tool.mcp_server))
            .join(ToolSet, ToolSet.id == ToolSetItem.tool_set_id)
            .where(
                ToolSetItem.tool_set_id == agent.tool_set_id,
                ToolSet.organization_id == agent.organization_id,
            )
        )
        rows: list[tuple[Tool, ToolSetItem]] = []
        for item in result.scalars().all():
            tool = item.tool
            if tool is None or tool.status != "active":
                continue
            rows.append((tool, item))
        rows.sort(key=lambda pair: pair[0].name)
        return rows

    async def _call(
        self,
        session: AsyncSession,
        user: User,
        agent: Agent,
        request_id: Any,
        *,
        name: str,
        arguments: dict[str, Any],
        run_id: str,
        action_id: str,
    ) -> dict[str, Any]:
        match = next(((tool, item) for tool, item in await self._exposed(session, agent) if tool.name == name), None)
        if match is None:
            return rpc_error(request_id, -32003, "Agent is not allowed to call this tool")
        tool, item = match
        await audit_service.log(
            session,
            event=AuditEvent.TOOL_REQUESTED,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            organization_id=agent.organization_id,
            tool=tool.name,
            result="requested",
            payload={"arguments": arguments, "run_id": run_id, "action_id": action_id},
        )
        decision = await policy_engine.evaluate_named(
            session,
            organization_id=agent.organization_id,
            agent=agent,
            tool_name=tool.name,
            arguments=arguments,
        )
        await audit_service.log(
            session,
            event=AuditEvent.POLICY_EVALUATED,
            actor_type=ActorType.SYSTEM,
            actor_id="policy_engine",
            organization_id=agent.organization_id,
            tool=tool.name,
            result=decision.decision.value,
            payload={
                "policy_id": decision.policy_id,
                "policy_version": decision.policy_version,
                "matched_rule_id": decision.matched_rule_id,
                "reason": decision.reason,
                "arguments": arguments,
                "agent_id": agent.id,
                "run_id": run_id,
                "action_id": action_id,
            },
        )
        if decision.decision == PolicyDecision.DENY:
            return rpc_error(request_id, -32004, "policy_denied", {"reason": decision.reason or "Policy denied"})
        if decision.decision == PolicyDecision.APPROVAL_REQUIRED or item.approval:
            return await self._approval_call(
                session,
                user,
                agent,
                tool,
                request_id,
                arguments=arguments,
                run_id=run_id,
                action_id=action_id,
            )
        result = await self._proxy(tool, arguments)
        await audit_service.log(
            session,
            event=AuditEvent.TOOL_EXECUTED,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            organization_id=agent.organization_id,
            tool=tool.name,
            result="completed",
            payload={"arguments": arguments, "run_id": run_id, "action_id": action_id},
        )
        return rpc_result(request_id, result)

    async def _approval_call(
        self,
        session: AsyncSession,
        user: User,
        agent: Agent,
        tool: Tool,
        request_id: Any,
        *,
        arguments: dict[str, Any],
        run_id: str,
        action_id: str,
    ) -> dict[str, Any]:
        action = await session.get(Action, action_id) if action_id else None
        if action is not None:
            approval = await session.scalar(select(Approval).where(Approval.action_id == action.id))
            if approval is not None and approval.status == ApprovalStatus.APPROVED.value:
                result = await self._proxy(tool, arguments)
                action.status = ActionStatus.COMPLETED.value
                action.result = result
                run = await session.get(Run, action.run_id)
                if run is not None:
                    run.status = RunStatus.COMPLETED.value
                await audit_service.log(
                    session,
                    event=AuditEvent.TOOL_EXECUTED,
                    actor_type=ActorType.AGENT,
                    actor_id=agent.id,
                    organization_id=agent.organization_id,
                    run_id=action.run_id,
                    action_id=action.id,
                    tool=tool.name,
                    result="completed",
                    payload={"arguments": arguments, "approval_id": approval.id},
                )
                return rpc_result(request_id, result)
            if approval is not None and approval.status == ApprovalStatus.PENDING.value:
                return rpc_error(
                    request_id,
                    -32001,
                    "approval_required",
                    {"approval_id": approval.id, "run_id": action.run_id, "action_id": action.id},
                )
            if approval is not None and approval.status == ApprovalStatus.REJECTED.value:
                return rpc_error(request_id, -32002, "approval_rejected")

        ident = run_id or new_id("run")
        run = await session.get(Run, ident)
        if run is None:
            run = Run(
                id=ident,
                organization_id=agent.organization_id,
                agent_id=agent.id,
                input=tool.name,
                status=RunStatus.WAITING_APPROVAL.value,
            )
            session.add(run)
            await session.flush()
        elif run.organization_id != agent.organization_id or run.agent_id != agent.id:
            return rpc_error(request_id, -32000, "Run does not belong to this agent")
        else:
            run.status = RunStatus.WAITING_APPROVAL.value

        action_ident = action_id or new_id("act")
        if action is None:
            taken = await session.get(Action, action_ident)
            if taken is not None:
                action_ident = new_id("act")
            action = Action(
                id=action_ident,
                run_id=run.id,
                tool=tool.name,
                arguments=arguments,
                policy_decision="approval_required",
                policy_reason="saturn_mcp",
                status=ActionStatus.WAITING_APPROVAL.value,
            )
            session.add(action)
            await session.flush()
        approval = Approval(
            id=new_id("approval"),
            run_id=run.id,
            action_id=action.id,
            status=ApprovalStatus.PENDING.value,
        )
        session.add(approval)
        await session.flush()
        await audit_service.log(
            session,
            event=AuditEvent.APPROVAL_REQUESTED,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            organization_id=agent.organization_id,
            run_id=run.id,
            action_id=action.id,
            tool=tool.name,
            result="pending",
            payload={"approval_id": approval.id, "arguments": arguments, "user_id": user.id},
        )
        return rpc_error(
            request_id,
            -32001,
            "approval_required",
            {"approval_id": approval.id, "run_id": run.id, "action_id": action.id},
        )

    async def _proxy(self, tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
        server = tool.mcp_server
        endpoint = (server.endpoint if server is not None else "") or tool.mcp_endpoint
        secret = open_secret(server.auth_ciphertext) if server is not None else ""
        upstream = tool.upstream_name or tool.name
        return await call_remote_tool(endpoint, upstream, arguments, secret)


saturn_mcp = SaturnMcp()

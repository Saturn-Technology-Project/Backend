from __future__ import annotations

import hashlib
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ids import new_id, utcnow
from app.models import Action, Agent, Approval, AuditLog, Run, Tool, ToolSet, ToolSetItem, User
from app.models.enums import ActionStatus, ActorType, ApprovalStatus, AuditEvent, PolicyDecision, RunStatus
from app.services.audit_service import audit_service
from app.services.mcp_remote import call_remote_tool
from app.services.policy_engine import policy_engine


def rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _split_remote(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Real MCP replies are a JSON-RPC envelope. Test doubles return the tool result itself."""
    if "result" in body and ("jsonrpc" in body or "id" in body):
        result = body.get("result")
        tool = result if isinstance(result, dict) else ({"value": result} if result is not None else {})
        return body, tool
    return body, body


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
        profile_id: str = "",
        client_name: str = "",
        client_id: str = "",
    ) -> dict[str, Any]:
        request_id = message.get("id", 1)
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        await self._bind_profile(session, agent_id, user, profile_id)
        if method == "initialize" and profile_id:
            info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
            if client_name and not str(info.get("name") or "").strip():
                params = {**params, "clientInfo": {"name": client_name}}
            await self._connect_client(session, user, agent_id, profile_id, params, client_id)
        if method == "initialize":
            return rpc_result(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "Saturn MCP", "version": "0.1.0"},
                },
            )
        agent = await self._client_for_call(session, user, agent_id, profile_id, client_name)
        if agent is None and profile_id and client_name:
            await self._connect_client(
                session,
                user,
                "",
                profile_id,
                {"clientInfo": {"name": client_name}},
                client_id,
            )
            agent = await self._client_for_call(session, user, "", profile_id, client_name)
        if agent is not None and str((agent.settings or {}).get("origin") or "") == "saturn_mcp":
            agent.last_seen_at = utcnow()
            await session.flush()
        if method in {"notifications/initialized", "ping"}:
            return rpc_result(request_id, {})
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
                message=message,
            )
        return rpc_error(request_id, -32601, "Method not found")

    async def _client_for_call(
        self,
        session: AsyncSession,
        user: User,
        agent_id: str,
        profile_id: str,
        client_name: str,
    ) -> Agent | None:
        agent = await session.get(Agent, agent_id) if agent_id else None
        if agent is None and not agent_id and profile_id:
            if client_name:
                agent = await self._client_agent(session, user, profile_id, client_name)
            if agent is None:
                matches = await self._profile_agents(session, user, profile_id)
                if not client_name or len(matches) == 1:
                    agent = matches[0] if matches else None
        if agent is None or agent.organization_id != user.organization_id:
            return None
        return agent

    def _client_id(self, profile_id: str, name: str) -> str:
        digest = hashlib.sha256(f"{profile_id}\n{name}".encode()).hexdigest()[:24]
        return f"mcp_{digest}"

    async def _connect_client(
        self,
        session: AsyncSession,
        user: User,
        agent_id: str,
        profile_id: str,
        params: dict[str, Any],
        client_id: str = "",
    ) -> None:
        info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        name = str(info.get("name") or "Agent").strip()[:255] or "Agent"
        existing = await session.get(Agent, agent_id) if agent_id else None
        if existing is not None and existing.organization_id != user.organization_id:
            return
        if existing is not None and str((existing.settings or {}).get("origin") or "") != "saturn_mcp":
            return
        agent = existing
        if agent is None:
            ident = agent_id or self._client_id(profile_id, name)
            agent = await session.get(Agent, ident)
            if agent is not None and agent.organization_id != user.organization_id:
                return
            if agent is not None and str((agent.settings or {}).get("origin") or "") != "saturn_mcp":
                return
        if agent is None:
            agent = Agent(
                id=agent_id or self._client_id(profile_id, name),
                organization_id=user.organization_id,
                name=name,
                description="",
                model="",
                settings={},
            )
            session.add(agent)
        settings = dict(agent.settings or {})
        settings["origin"] = "saturn_mcp"
        settings["profile_id"] = profile_id
        settings["registered_user_id"] = user.id
        if client_id:
            settings["client_id"] = client_id
        agent.settings = settings
        agent.name = name
        agent.last_seen_at = utcnow()
        await session.flush()

    async def _profile_agents(self, session: AsyncSession, user: User, profile_id: str) -> list[Agent]:
        result = await session.execute(
            select(Agent)
            .where(Agent.organization_id == user.organization_id)
            .order_by(Agent.last_seen_at.desc())
        )
        matched: list[Agent] = []
        for agent in result.scalars().all():
            settings = agent.settings or {}
            if settings.get("origin") == "saturn_mcp" and settings.get("profile_id") == profile_id:
                matched.append(agent)
        return matched

    async def _client_agent(
        self,
        session: AsyncSession,
        user: User,
        profile_id: str,
        client_name: str,
    ) -> Agent | None:
        name = client_name.strip()[:255] or "Agent"
        direct = await session.get(Agent, self._client_id(profile_id, name))
        if self._same_client(direct, user, profile_id):
            return direct
        for agent in await self._profile_agents(session, user, profile_id):
            if agent.name == name:
                return agent
        return None

    def _same_client(self, agent: Agent | None, user: User, profile_id: str) -> bool:
        if agent is None or agent.organization_id != user.organization_id:
            return False
        settings = agent.settings or {}
        return settings.get("origin") == "saturn_mcp" and settings.get("profile_id") == profile_id

    async def _bind_profile(
        self,
        session: AsyncSession,
        agent_id: str,
        user: User,
        profile_id: str,
    ) -> None:
        if not profile_id or not agent_id:
            return
        agent = await session.get(Agent, agent_id)
        if agent is None or agent.organization_id != user.organization_id:
            return
        settings = dict(agent.settings or {})
        if settings.get("profile_id") == profile_id:
            return
        settings["profile_id"] = profile_id
        agent.settings = settings
        await session.flush()

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

    async def _next_audit_no(self, session: AsyncSession, organization_id: str) -> int:
        count = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.organization_id == organization_id,
                AuditLog.event == AuditEvent.TOOL_REQUESTED.value,
            )
        )
        return int(count or 0) + 1

    async def _audit(self, session: AsyncSession, **kwargs: Any) -> None:
        payload = kwargs.get("payload") if isinstance(kwargs.get("payload"), dict) else {}
        kwargs["payload"] = {**payload, "source": "saturn_mcp"}
        await audit_service.log(session, **kwargs)

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
        message: dict[str, Any],
    ) -> dict[str, Any]:
        match = next(((tool, item) for tool, item in await self._exposed(session, agent) if tool.name == name), None)
        if match is None:
            return rpc_error(request_id, -32003, "Agent is not allowed to call this tool")
        tool, item = match
        audit_no = await self._next_audit_no(session, agent.organization_id)
        server_name = tool.mcp_server.name if tool.mcp_server is not None else ""
        stamp = {"audit_no": audit_no, "run_id": run_id, "action_id": action_id, "server_name": server_name}
        await self._audit(
            session,
            event=AuditEvent.TOOL_REQUESTED,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            organization_id=agent.organization_id,
            tool=tool.name,
            result="requested",
            payload={**stamp, "arguments": arguments, "raw_request": message},
        )
        decision = await policy_engine.evaluate_named(
            session,
            organization_id=agent.organization_id,
            agent=agent,
            tool_name=tool.name,
            arguments=arguments,
        )
        await self._audit(
            session,
            event=AuditEvent.POLICY_EVALUATED,
            actor_type=ActorType.SYSTEM,
            actor_id="policy_engine",
            organization_id=agent.organization_id,
            tool=tool.name,
            result=decision.decision.value,
            payload={
                "policy_id": decision.policy_id,
                "policy_name": decision.policy_name,
                "policy_version": decision.policy_version,
                "matched_rule_id": decision.matched_rule_id,
                "reason": decision.reason,
                "arguments": arguments,
                "agent_id": agent.id,
                "run_id": run_id,
                "action_id": action_id,
                "audit_no": audit_no,
                "server_name": server_name,
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
                audit_no=audit_no,
            )
        raw_response = await self._proxy(session, tool, arguments)
        raw_response, result = _split_remote(raw_response)
        reply = rpc_result(request_id, result)
        await self._audit(
            session,
            event=AuditEvent.TOOL_EXECUTED,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            organization_id=agent.organization_id,
            run_id=run_id,
            action_id=action_id,
            tool=tool.name,
            result="completed",
            payload={
                **stamp,
                "arguments": arguments,
                "raw_response": raw_response,
                "server_result": result,
                "agent_result": reply,
            },
        )
        return reply

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
        audit_no: int,
    ) -> dict[str, Any]:
        action = await session.get(Action, action_id) if action_id else None
        if action is not None:
            approval = await session.scalar(select(Approval).where(Approval.action_id == action.id))
            if approval is not None and approval.status == ApprovalStatus.APPROVED.value:
                stored = action.arguments if isinstance(action.arguments, dict) else {}
                if stored != arguments:
                    return rpc_error(
                        request_id,
                        -32004,
                        "approval_mismatch",
                        {"reason": "Approved parameters no longer match this call"},
                    )
                fresh = await policy_engine.evaluate_named(
                    session,
                    organization_id=agent.organization_id,
                    agent=agent,
                    tool_name=tool.name,
                    arguments=arguments,
                )
                if fresh.decision == PolicyDecision.DENY:
                    return rpc_error(
                        request_id,
                        -32004,
                        "policy_denied",
                        {"reason": fresh.reason or "Policy denied"},
                    )
                raw_response = await self._proxy(session, tool, arguments)
                raw_response, result = _split_remote(raw_response)
                reply = rpc_result(request_id, result)
                action.status = ActionStatus.COMPLETED.value
                action.result = result
                run = await session.get(Run, action.run_id)
                if run is not None:
                    run.status = RunStatus.COMPLETED.value
                await self._audit(
                    session,
                    event=AuditEvent.TOOL_EXECUTED,
                    actor_type=ActorType.AGENT,
                    actor_id=agent.id,
                    organization_id=agent.organization_id,
                    run_id=action.run_id,
                    action_id=action.id,
                    tool=tool.name,
                    result="completed",
                    payload={
                        "audit_no": audit_no,
                        "server_name": tool.mcp_server.name if tool.mcp_server is not None else "",
                        "arguments": arguments,
                        "approval_id": approval.id,
                        "raw_response": raw_response,
                        "server_result": result,
                        "agent_result": reply,
                    },
                )
                return reply
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
        await self._audit(
            session,
            event=AuditEvent.APPROVAL_REQUESTED,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            organization_id=agent.organization_id,
            run_id=run.id,
            action_id=action.id,
            tool=tool.name,
            result="pending",
            payload={
                "audit_no": audit_no,
                "server_name": tool.mcp_server.name if tool.mcp_server is not None else "",
                "approval_id": approval.id,
                "arguments": arguments,
                "user_id": user.id,
            },
        )
        return rpc_error(
            request_id,
            -32001,
            "approval_required",
            {"approval_id": approval.id, "run_id": run.id, "action_id": action.id},
        )

    async def _proxy(self, session: AsyncSession, tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
        from app.services.mcp_oauth import mcp_oauth
        from app.services.mcp_remote import McpUnauthorized

        server = tool.mcp_server
        endpoint = (server.endpoint if server is not None else "") or tool.mcp_endpoint
        secret = await mcp_oauth.authorization_for(session, server)
        upstream = tool.upstream_name or tool.name
        try:
            return await call_remote_tool(endpoint, upstream, arguments, secret)
        except McpUnauthorized:
            secret = await mcp_oauth.refresh_after_reject(session, server)
            if not secret:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "MCP server returned HTTP 401")
            try:
                return await call_remote_tool(endpoint, upstream, arguments, secret)
            except McpUnauthorized as exc:
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "MCP server returned HTTP 401") from exc


saturn_mcp = SaturnMcp()

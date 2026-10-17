from __future__ import annotations

import hashlib
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.ids import new_id, utcnow
from app.models import Action, Agent, Approval, Run, Tool, ToolSet, ToolSetItem, User
from app.core.enums import ActionStatus, ActorType, ApprovalStatus, AuditEvent, PolicyDecision, RunStatus
from app.features.execution.activity.services.audit_service import audit_service
from app.features.execution.activity.services.zero_access import PAYLOAD_ENCRYPTION, action_digest, is_zero_access
from app.features.execution.activity.services.content_seal import plain_arguments, policy_fields
from app.features.mcp.saturn.services.mcp_sessions import mcp_sessions
from app.features.mcp.servers.services.mcp_remote import call_remote_tool
from app.features.policy.policies.services.policy_engine import policy_engine


def rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _split_remote(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Real MCP replies are a JSON-RPC envelope. Test doubles return the tool result itself."""
    if "result" in body and ("jsonrpc" in body or "id" in body):
        result = body.get("result")
        tool = result if isinstance(result, dict) else ({"value": result} if result is not None else {})
        return body, tool
    return body, body


def _status_text(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value or "")


SATURN_RUN_ARGUMENT = "saturnRunId"


def _with_run_argument(schema: dict[str, Any] | None) -> dict[str, Any]:
    base = dict(schema) if isinstance(schema, dict) else {}
    if base.get("type") not in {None, "object"}:
        return base
    properties = dict(base.get("properties") or {})
    properties.setdefault(
        SATURN_RUN_ARGUMENT,
        {
            "type": "string",
            "description": (
                "Leave this out on the first call. If Saturn returns approval_required, wait for approval, "
                "then call this same tool again with the same arguments and set saturnRunId to the value in that error. "
                "If Saturn returns approval_rejected for that id, leave saturnRunId out to start a separate request."
            ),
        },
    )
    base["type"] = "object"
    base["properties"] = properties
    return base


def _take_run_argument(arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
    clean = dict(arguments)
    raw = clean.pop(SATURN_RUN_ARGUMENT, None)
    return clean, raw.strip() if isinstance(raw, str) else ""


def _approval_notice(
    request_id: Any,
    code: int,
    message: str,
    approval: Approval,
    tool_name: str,
    *,
    status: str,
    reason: str = "",
    phase: str = "",
    policy_id: str | None = None,
) -> dict[str, Any]:
    data = {
        "approval_id": approval.id,
        "run_id": approval.run_id,
        "action_id": approval.action_id,
        "tool": tool_name,
        "status": status,
        "saturnRunId": approval.run_id,
        "argument": SATURN_RUN_ARGUMENT,
    }
    if reason:
        data["reason"] = reason
    if phase:
        data["phase"] = phase
    if status == "pending":
        from app.features.mcp.saturn.services.agent_messages import pending_fields

        data.update(pending_fields(message, approval.id, policy_id))
    return rpc_error(request_id, code, message, data)


def _approval_required_message(tool_name: str, run_id: str) -> str:
    from app.features.mcp.saturn.services.agent_messages import approval_pending

    return approval_pending(tool_name, run_id)


def _approval_rejected_message(run_id: str) -> str:
    return (
        f'approval_rejected. A person rejected saturnRunId "{run_id}". '
        "Do not call this tool again with that saturnRunId. "
        "To ask again, call it once more and leave saturnRunId out."
    )


def _approval_expired(request_id: Any, approval: Approval, phase: str, tool_name: str = "") -> dict[str, Any]:
    phase = phase or "pending"
    if phase == "resubmit":
        message = (
            f'approval_expired. The resubmit window for saturnRunId "{approval.run_id}" has passed. '
            "Leave saturnRunId out to start a separate request."
        )
    else:
        message = (
            f'approval_expired. saturnRunId "{approval.run_id}" expired before anyone approved it. '
            "Leave saturnRunId out to start a separate request."
        )
    return _approval_notice(
        request_id,
        -32006,
        message,
        approval,
        tool_name,
        status="expired",
        reason="APPROVAL_EXPIRED",
        phase=phase,
    )


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
        message: dict[str, Any],
        profile_id: str = "",
        client_name: str = "",
        client_id: str = "",
        mcp_session_id: str = "",
    ) -> dict[str, Any]:
        request_id = message.get("id", 1)
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        await self._bind_profile(session, agent_id, user, profile_id, client_id)
        if method == "initialize" and profile_id:
            await self._connect_client(session, user, agent_id, profile_id, params, client_id, client_name)
        agent = await self._client_for_call(session, user, agent_id, profile_id, client_name, client_id)
        if agent is None and profile_id and client_name and not agent_id:
            await self._connect_client(
                session,
                user,
                "",
                profile_id,
                {"clientInfo": {"name": client_name}},
                client_id,
                client_name,
            )
            agent = await self._client_for_call(session, user, "", profile_id, client_name, client_id)
        if agent is not None and str((agent.settings or {}).get("origin") or "") == "saturn_mcp":
            agent.last_seen_at = utcnow()
            await session.flush()
        if mcp_session_id and agent is not None and self._owns(agent, user, client_id):
            await mcp_sessions.open(mcp_session_id, user.id, agent.id, client_id)
        if method == "initialize":
            return rpc_result(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "Saturn MCP", "version": "0.1.0"},
                },
            )
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
                            "inputSchema": _with_run_argument(tool.input_schema),
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
        client_id: str,
    ) -> Agent | None:
        if agent_id:
            agent = await session.get(Agent, agent_id)
            return agent if self._owns(agent, user, client_id) else None
        if not profile_id or not client_name or not client_id:
            return None
        return await self._client_agent(session, user, profile_id, client_name, client_id)

    def _owns(self, agent: Agent | None, user: User, client_id: str) -> bool:
        if agent is None or agent.organization_id != user.organization_id or not client_id:
            return False
        settings = agent.settings or {}
        return (
            settings.get("origin") == "saturn_mcp"
            and settings.get("registered_user_id") == user.id
            and settings.get("client_id") == client_id
        )

    def _legacy_id(self, profile_id: str, name: str, client_id: str) -> str:
        digest = hashlib.sha256(f"{profile_id}\n{client_id}\n{name}".encode()).hexdigest()[:24]
        return f"mcp_{digest}"

    def _stable_id(self, profile_id: str, user_id: str, name: str) -> str:
        digest = hashlib.sha256(f"{profile_id}\n{user_id}\n{name}".encode()).hexdigest()[:24]
        return f"mcp_{digest}"

    def _product_name(self, params: dict[str, Any], client_name: str) -> str:
        info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        name = str(info.get("name") or client_name or "Agent").strip()
        return name[:255] or "Agent"

    async def _connect_client(
        self,
        session: AsyncSession,
        user: User,
        agent_id: str,
        profile_id: str,
        params: dict[str, Any],
        client_id: str = "",
        client_name: str = "",
    ) -> None:
        if not client_id:
            return
        name = self._product_name(params, client_name)
        existing = await session.get(Agent, agent_id) if agent_id else None
        if existing is not None and not self._owns(existing, user, client_id):
            return
        agent = existing or await self._find_client_agent(session, user, profile_id, name, client_id)
        if agent is None:
            agent = Agent(
                id=self._stable_id(profile_id, user.id, name),
                organization_id=user.organization_id,
                name=name,
                description="",
                model="",
                settings={},
            )
            session.add(agent)
        settings = dict(agent.settings or {})
        current_profile = str(settings.get("profile_id") or "")
        current_user = str(settings.get("registered_user_id") or "")
        if current_profile and current_profile != profile_id:
            return
        if current_user and current_user != user.id:
            return
        settings["origin"] = "saturn_mcp"
        settings["profile_id"] = current_profile or profile_id
        settings["registered_user_id"] = user.id
        settings["client_id"] = client_id
        settings["client_name"] = name
        agent.settings = settings
        agent.name = name
        agent.last_seen_at = utcnow()
        await session.flush()

    async def _find_client_agent(
        self,
        session: AsyncSession,
        user: User,
        profile_id: str,
        name: str,
        client_id: str,
    ) -> Agent | None:
        for ident in (self._stable_id(profile_id, user.id, name), self._legacy_id(profile_id, name, client_id)):
            direct = await session.get(Agent, ident)
            if self._same_client(direct, user, profile_id, name):
                return direct
        matches = [
            agent
            for agent in await self._profile_agents(session, user, profile_id)
            if self._same_client(agent, user, profile_id, name)
        ]
        if not matches:
            return None
        matches.sort(key=lambda agent: (agent.tool_set_id is None, agent.created_at))
        return matches[0]

    def _same_client(self, agent: Agent | None, user: User, profile_id: str, name: str) -> bool:
        if agent is None or agent.organization_id != user.organization_id:
            return False
        settings = agent.settings or {}
        if settings.get("origin") != "saturn_mcp":
            return False
        if settings.get("registered_user_id") != user.id:
            return False
        if str(settings.get("profile_id") or "") not in {"", profile_id}:
            return False
        stored = str(settings.get("client_name") or agent.name or "")
        return stored == name

    async def _client_agent(
        self,
        session: AsyncSession,
        user: User,
        profile_id: str,
        client_name: str,
        client_id: str,
    ) -> Agent | None:
        name = client_name.strip()[:255] or "Agent"
        found = await self._find_client_agent(session, user, profile_id, name, client_id)
        if found is not None and self._owns(found, user, client_id):
            return found
        return None

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

    async def _bind_profile(
        self,
        session: AsyncSession,
        agent_id: str,
        user: User,
        profile_id: str,
        client_id: str,
    ) -> None:
        if not profile_id or not agent_id or not client_id:
            return
        agent = await session.get(Agent, agent_id)
        if not self._owns(agent, user, client_id):
            return
        settings = dict(agent.settings or {})
        if settings.get("profile_id"):
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
        event = AuditEvent.TOOL_REQUESTED.value
        value = await session.scalar(
            text(
                """
                INSERT INTO audit_counters (organization_id, event, value)
                VALUES (
                    :org,
                    :event,
                    (SELECT COALESCE(COUNT(*), 0) + 1 FROM audit_logs
                     WHERE organization_id = :org AND event = :event)
                )
                ON CONFLICT (organization_id, event)
                DO UPDATE SET value = audit_counters.value + 1
                RETURNING value
                """
            ),
            {"org": organization_id, "event": event},
        )
        return int(value or 1)

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
        message: dict[str, Any],
    ) -> dict[str, Any]:
        arguments, run_id = _take_run_argument(arguments)
        action_id = ""
        match = next(((tool, item) for tool, item in await self._exposed(session, agent) if tool.name == name), None)
        if match is None:
            audit_no = await self._next_audit_no(session, agent.organization_id)
            await self._audit(
                session,
                event=AuditEvent.TOOL_REQUESTED,
                actor_type=ActorType.AGENT,
                actor_id=agent.id,
                organization_id=agent.organization_id,
                tool=name,
                result="denied",
                payload={
                    "audit_no": audit_no,
                    "run_id": run_id,
                    "action_id": action_id,
                    "arguments": arguments,
                    "raw_request": message,
                    "reason": "Agent is not allowed to call this tool",
                    "code": -32003,
                },
            )
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
            payload={**stamp, "arguments": arguments, "raw_request": message, "policy_fields": []},
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
                "policy_fields": policy_fields(decision.matched_rule),
                "agent_id": agent.id,
                "run_id": run_id,
                "action_id": action_id,
                "audit_no": audit_no,
                "server_name": server_name,
            },
        )
        if decision.decision == PolicyDecision.DENY:
            from app.features.mcp.saturn.services.agent_messages import deny_error

            return deny_error(request_id, reason=decision.reason, policy_id=decision.policy_id)
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
                decision=decision,
            )
        raw_response = await self._proxy(session, tool, arguments)
        raw_response, result = _split_remote(raw_response)
        reply = rpc_result(request_id, result)
        # saturnRunId is the only caller-supplied run id. Link the audit row when that run belongs to this agent.
        run = await session.get(Run, run_id) if run_id else None
        action = await session.get(Action, action_id) if action_id else None
        await self._audit(
            session,
            event=AuditEvent.TOOL_EXECUTED,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            organization_id=agent.organization_id,
            run_id=run.id if run is not None and run.organization_id == agent.organization_id else None,
            action_id=action.id if action is not None and run is not None and action.run_id == run.id else None,
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
        decision=None,
    ) -> dict[str, Any]:
        from app.features.execution.approvals.services.approval_service import approval_service, parameter_digest

        action = await session.get(Action, action_id) if action_id else None
        if action is not None:
            approval = await session.scalar(select(Approval).where(Approval.action_id == action.id))
            if approval is not None:
                from app.features.execution.approvals.services.expiration import expire_if_due

                phase = await expire_if_due(session, approval, utcnow())
                if phase:
                    return _approval_expired(request_id, approval, phase, tool.name)
            if approval is not None and approval.status == ApprovalStatus.APPROVED.value:
                digest = action_digest(action.arguments) or (approval.parameter_digest or "")
                if digest:
                    if digest != parameter_digest(arguments):
                        from app.features.mcp.saturn.services.agent_messages import mismatch_error

                        return mismatch_error(request_id, policy_id=getattr(decision, "policy_id", None))
                else:
                    stored = plain_arguments(agent.organization_id, action.arguments)
                    if stored != arguments:
                        from app.features.mcp.saturn.services.agent_messages import mismatch_error

                        return mismatch_error(request_id, policy_id=getattr(decision, "policy_id", None))
                if approval.execution_state == "executed":
                    cached = action.result if isinstance(action.result, dict) else {}
                    return rpc_result(request_id, cached)
                if approval.execution_state == "uncertain":
                    return rpc_error(request_id, -32005, "execution_uncertain")
                if approval.execution_state == "failed":
                    return rpc_error(request_id, -32005, "execution_failed")
                if approval.execution_state == "claimed":
                    return rpc_error(request_id, -32005, "execution_in_progress")
                claimed = await session.execute(
                    update(Approval)
                    .where(
                        Approval.id == approval.id,
                        Approval.status == ApprovalStatus.APPROVED.value,
                        Approval.execution_state == "none",
                    )
                    .values(execution_state="claimed")
                )
                if claimed.rowcount != 1:
                    await session.refresh(approval)
                    if _status_text(approval.status) == ApprovalStatus.EXPIRED.value:
                        return _approval_expired(request_id, approval, approval.expired_phase or "resubmit", tool.name)
                    return rpc_error(request_id, -32005, "execution_in_progress")
                approval.execution_state = "claimed"
                fresh = await policy_engine.evaluate_named(
                    session,
                    organization_id=agent.organization_id,
                    agent=agent,
                    tool_name=tool.name,
                    arguments=arguments,
                )
                if fresh.decision == PolicyDecision.DENY:
                    from app.features.mcp.saturn.services.agent_messages import deny_error

                    return deny_error(request_id, reason=fresh.reason, policy_id=fresh.policy_id)
                raw_response = await self._proxy(session, tool, arguments)
                raw_response, result = _split_remote(raw_response)
                reply = rpc_result(request_id, result)
                action.status = ActionStatus.COMPLETED.value
                action.result = result
                approval.execution_state = "executed"
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
                if approval.parameter_digest and approval.parameter_digest != parameter_digest(arguments):
                    approval.status = ApprovalStatus.INVALIDATED.value
                    action.status = ActionStatus.DENIED.value
                    from app.features.mcp.saturn.services.agent_messages import mismatch_error

                    return mismatch_error(request_id, policy_id=getattr(decision, "policy_id", None))
                return _approval_notice(
                    request_id,
                    -32001,
                    _approval_required_message(tool.name, action.run_id),
                    approval,
                    tool.name,
                    status="pending",
                    policy_id=getattr(decision, "policy_id", None),
                )
            if approval is not None and _status_text(approval.status) == ApprovalStatus.EXPIRED.value:
                return _approval_expired(request_id, approval, approval.expired_phase or "pending", tool.name)
            if approval is not None and approval.status in {
                ApprovalStatus.REJECTED.value,
                ApprovalStatus.CANCELLED.value,
                ApprovalStatus.INVALIDATED.value,
            }:
                closed = _status_text(approval.status)
                text = (
                    _approval_rejected_message(approval.run_id)
                    if closed == ApprovalStatus.REJECTED.value
                    else f"approval_{closed}. saturnRunId \"{approval.run_id}\" is closed. Leave saturnRunId out to start a separate request."
                )
                return _approval_notice(
                    request_id,
                    -32002,
                    text,
                    approval,
                    tool.name,
                    status=closed,
                )

        if not action_id and run_id:
            owned = await session.get(Run, run_id)
            if owned is not None and owned.organization_id == agent.organization_id and owned.agent_id == agent.id:
                action = await session.scalar(
                    select(Action)
                    .where(Action.run_id == owned.id, Action.tool == tool.name)
                    .order_by(Action.created_at.desc())
                )
                if action is not None:
                    return await self._approval_call(
                        session,
                        user,
                        agent,
                        tool,
                        request_id,
                        arguments=arguments,
                        run_id=run_id,
                        action_id=action.id,
                        audit_no=audit_no,
                        decision=decision,
                    )

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
            from app.features.execution.activity.services.zero_access import protect_mapping
            from app.features.execution.approvals.services.approval_service import parameter_digest as digest_of

            envelope = await protect_mapping(session, agent.organization_id, action_ident, "action", arguments)
            stored_arguments: dict[str, Any] = {"digest": digest_of(arguments)}
            if envelope is None:
                stored_arguments["encryption"] = "unavailable"
            else:
                stored_arguments["envelope"] = envelope
                stored_arguments["encryption"] = PAYLOAD_ENCRYPTION
            action = Action(
                id=action_ident,
                run_id=run.id,
                tool=tool.name,
                arguments=stored_arguments,
                policy_decision="approval_required",
                policy_id=getattr(decision, "policy_id", None) if decision is not None else None,
                policy_version=getattr(decision, "policy_version", None) if decision is not None else None,
                policy_rule_id=getattr(decision, "matched_rule_id", None) if decision is not None else None,
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
        if decision is not None:
            await approval_service.configure_request(
                session,
                approval,
                organization_id=agent.organization_id,
                decision=decision,
                arguments=arguments,
                requester_user_id=user.id,
            )
        else:
            from app.features.execution.approvals.services.expiration import arm_pending

            await arm_pending(session, approval, agent.organization_id)
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
                "policy_fields": policy_fields(getattr(decision, "matched_rule", None)),
                "user_id": user.id,
            },
        )
        return _approval_notice(
            request_id,
            -32001,
            _approval_required_message(tool.name, run.id),
            approval,
            tool.name,
            status="pending",
            policy_id=getattr(decision, "policy_id", None),
        )

    async def _proxy(self, session: AsyncSession, tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
        from app.features.mcp.oauth.services.mcp_oauth import mcp_oauth
        from app.features.mcp.servers.services.mcp_remote import McpUnauthorized

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

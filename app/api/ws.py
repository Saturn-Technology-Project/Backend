from __future__ import annotations

import hmac
import json
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.api.deps import SessionDep
from app.api.runs import serialize_run
from app.core.ids import new_id, utcnow
from app.models import Agent, User
from app.models.enums import ActorType, AuditEvent
from app.services.access import allows
from app.services.agent_hub import agent_hub
from app.services.audit_service import audit_service
from app.services.auth_service import auth_service, hash_token
from app.services.connector_service import connector_service
from app.services.mapping_service import mapping_service
from app.services.policy_engine import policy_engine
from app.services.runtime_bridge import apply_runtime_result
from app.services.run_service import run_service

router = APIRouter(tags=["websocket"])


def _run_payload(run) -> dict[str, Any]:
    return serialize_run(run).model_dump(mode="json")


def _error_detail(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, str):
        return detail
    return str(detail)


_RUN_EVENTS = {
    "result",
    "run.started",
    "run.progress",
    "run.waiting_approval",
    "run.completed",
    "run.failed",
}


async def _release(session) -> None:
    if session.in_transaction():
        await session.commit()


def _command_id(message: dict[str, Any]) -> str:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    return str(message.get("command_id") or payload.get("command_id") or message.get("id") or "")


async def _auth_agent(session, agent_id: str, token: str):
    agent = await session.get(Agent, agent_id)
    expected = agent.token_hash if agent is not None else ""
    if (
        agent is None
        or not expected
        or not hmac.compare_digest(hash_token(token), expected)
    ):
        return None
    return agent


@router.websocket("/ws/agents")
async def agent_socket(
    websocket: WebSocket,
    session: SessionDep,
    agent_id: str,
    token: str,
):
    await websocket.accept()
    agent = await _auth_agent(session, agent_id, token)
    if agent is None:
        await websocket.send_json({"type": "error", "error": "unauthorized"})
        await websocket.close(code=4401, reason="unauthorized")
        return
    await _bound_runtime_loop(
        websocket, session, agent, runtime_id="", greet=True
    )


@router.websocket("/ws/runtime")
async def runtime_socket(
    websocket: WebSocket,
    session: SessionDep,
    runtime_id: str = "",
    token: str = "",
    agent_id: str = "",
):
    await _runtime_connect(
        websocket, session, runtime_id=runtime_id, token=token, agent_id=agent_id
    )


@router.websocket("/runtime/connect")
async def runtime_connect_alias(
    websocket: WebSocket,
    session: SessionDep,
    runtime_id: str = "",
    token: str = "",
    agent_id: str = "",
):
    await _runtime_connect(
        websocket, session, runtime_id=runtime_id, token=token, agent_id=agent_id
    )


@router.websocket("/ws/connectors")
async def connector_socket(
    websocket: WebSocket,
    session: SessionDep,
    connector_id: str = "",
    token: str = "",
    organization_id: str = "",
):
    await websocket.accept()
    connector = None
    first_message = None
    if token:
        connector = await connector_service.get_by_token(session, token)
        if connector is None:
            await websocket.send_json({"type": "error", "error": "unauthorized"})
            await websocket.close(code=4401, reason="unauthorized")
            return
    else:
        try:
            raw = await websocket.receive_text()
            first_message = json.loads(raw)
        except (WebSocketDisconnect, json.JSONDecodeError):
            await websocket.send_json({"type": "error", "error": "unauthorized"})
            await websocket.close(code=4401, reason="unauthorized")
            return
        if not isinstance(first_message, dict):
            await websocket.send_json({"type": "error", "error": "unauthorized"})
            await websocket.close(code=4401, reason="unauthorized")
            return
        connector = await connector_service.get_by_token(
            session, str(first_message.get("token") or "")
        )
        if connector is None:
            await websocket.send_json({"type": "error", "error": "unauthorized"})
            await websocket.close(code=4401, reason="unauthorized")
            return
    await _connector_loop(
        websocket, session, connector, first_message=first_message
    )


async def _connector_loop(
    websocket: WebSocket, session, connector, *, first_message: dict[str, Any] | None = None
) -> None:
    connector.last_seen_at = utcnow()
    await session.flush()
    await _release(session)
    await agent_hub.register_connector(
        connector_id=connector.id,
        organization_id=connector.organization_id,
        websocket=websocket,
    )
    await websocket.send_json(
        {
            "type": "connection.ack",
            "connector_id": connector.id,
            "organization_id": connector.organization_id,
            "protocol": 2,
        }
    )
    await websocket.send_json(
        {
            "type": "connector.registered",
            "connector_id": connector.id,
            "organization_id": connector.organization_id,
            "protocol": 2,
        }
    )
    if first_message is not None:
        await _handle_connector_message(session, connector, websocket, first_message)
        await _release(session)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "error": "invalid_json"})
                continue
            if not isinstance(message, dict):
                continue
            await _handle_connector_message(session, connector, websocket, message)
            await _release(session)
    except WebSocketDisconnect:
        pass
    finally:
        await agent_hub.unregister_connector(connector.organization_id, websocket)


async def _handle_connector_message(session, connector, websocket, message: dict[str, Any]) -> None:
    kind = str(message.get("type") or "")
    runtime_id = str(message.get("runtime_id") or "")
    agent_hub.touch_connector(
        connector.organization_id,
        heartbeat=kind
        in {"heartbeat", "connector.heartbeat", "runtime.heartbeat", "mcp.heartbeat"},
    )
    connector.last_seen_at = utcnow()
    await session.flush()

    if kind in {"connection.init", "connector.register"}:
        capabilities = message.get("capabilities")
        await connector_service.touch(
            session,
            connector,
            environment_id=str(message.get("environment_id") or ""),
            version=str(message.get("version") or ""),
            capabilities=[str(item) for item in capabilities]
            if isinstance(capabilities, list)
            else None,
            remote_id=str(message.get("connector_id") or ""),
        )
        await session.commit()
        return

    if kind in {"heartbeat", "connector.heartbeat", "heartbeat_ack"}:
        await websocket.send_json(
            {"type": "connector.heartbeat.ack", "connector_id": connector.id}
        )
        await agent_hub.broadcast_org(
            connector.organization_id,
            {
                "type": "connector.heartbeat",
                "connector_id": connector.id,
                "last_heartbeat_at": utcnow().isoformat(),
            },
        )
        return

    if kind == "runtime.register":
        agents_raw = message.get("agents") if isinstance(message.get("agents"), list) else []
        agent_ids = [str(item) for item in agents_raw if isinstance(item, str) and item]
        agents = await connector_service.upsert_runtime_agents(
            session,
            connector,
            runtime_id=runtime_id,
            agents=agent_ids,
            name=str(message.get("name") or ""),
            role=str(message.get("role") or ""),
        )
        await session.commit()
        live_ids = [agent.id for agent in agents]
        await agent_hub.attach_connector_agents(
            organization_id=connector.organization_id,
            websocket=websocket,
            agent_ids=live_ids,
            runtime_id=runtime_id,
        )
        await websocket.send_json(
            {
                "type": "runtime.registered",
                "runtime_id": runtime_id or None,
                "organization_id": connector.organization_id,
                "connector_id": connector.id,
                "agents": live_ids,
            }
        )
        for agent_id in live_ids:
            await agent_hub.replay_pending(agent_id)
        await agent_hub.broadcast_org(
            connector.organization_id,
            {
                "type": "catalog.updated",
                "connector_id": connector.id,
                "agents": live_ids,
                "runtime_id": runtime_id or None,
            },
        )
        return

    if kind == "runtime.heartbeat":
        await websocket.send_json({"type": "runtime.heartbeat.ack", "runtime_id": runtime_id})
        for agent_id in agent_hub.connected_ids(connector.organization_id):
            if runtime_id and agent_hub.runtime_id(agent_id) != runtime_id:
                continue
            agent_hub.touch(agent_id, heartbeat=True, runtime_id=runtime_id)
            await agent_hub.broadcast_org(
                connector.organization_id,
                {
                    "type": "runtime.heartbeat",
                    "agent_id": agent_id,
                    "runtime_id": runtime_id or None,
                    "last_heartbeat_at": utcnow().isoformat(),
                },
            )
        return

    if kind == "mcp.register":
        tools_raw = message.get("tools") if isinstance(message.get("tools"), list) else []
        tools: list[str] = []
        for item in tools_raw:
            if isinstance(item, str):
                tools.append(item)
            elif isinstance(item, dict) and isinstance(item.get("tool_id"), str):
                tools.append(item["tool_id"])
        server = await connector_service.upsert_mcp(
            session,
            connector,
            server_id=str(message.get("server_id") or ""),
            tools=tools,
            url=str(message.get("url") or ""),
            name=str(message.get("name") or ""),
        )
        await session.commit()
        await websocket.send_json(
            {
                "type": "mcp.registered",
                "server_id": server.id,
                "tools": [tool.name for tool in (server.tools or [])],
                "connector_id": connector.id,
                "organization_id": connector.organization_id,
            }
        )
        await agent_hub.broadcast_org(
            connector.organization_id,
            {
                "type": "catalog.updated",
                "connector_id": connector.id,
                "mcp_server_id": server.id,
                "tools": [tool.name for tool in (server.tools or [])],
            },
        )
        return

    if kind == "mcp.heartbeat":
        await websocket.send_json(
            {"type": "mcp.heartbeat.ack", "server_id": str(message.get("server_id") or "")}
        )
        return

    if kind == "ping":
        await websocket.send_json({"type": "pong"})
        return

    if kind == "policy.evaluate":
        await _evaluate_policy(session, connector, websocket, message)
        await session.commit()
        return

    agent_id = str(message.get("agent_id") or "")
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    if not agent_id:
        agent_id = str(payload.get("agent_id") or "")
    agent = await session.get(Agent, agent_id) if agent_id else None
    if agent is None:
        run_id = str(message.get("run_id") or payload.get("run_id") or "")
        if run_id:
            try:
                run = await run_service.load_run(session, run_id)
            except HTTPException:
                run = None
            if run is not None:
                agent = await session.get(Agent, run.agent_id)
    if agent is None or agent.organization_id != connector.organization_id:
        return
    await _handle_agent_message(
        session, agent, message, runtime_id=runtime_id or agent.runtime_id or ""
    )


async def _evaluate_policy(session, connector, websocket, message: dict[str, Any]) -> None:
    request_id = str(message.get("request_id") or "")
    agent_id = str(message.get("agent_id") or "")
    tool_name = str(message.get("tool_id") or message.get("tool") or "")
    arguments = message.get("arguments") if isinstance(message.get("arguments"), dict) else {}
    agent = await session.get(Agent, agent_id) if agent_id else None
    status = "deny"
    reason = "Unknown agent"
    policy_id = None
    if agent is not None and agent.organization_id == connector.organization_id and tool_name:
        result = await policy_engine.evaluate_named(
            session,
            organization_id=connector.organization_id,
            agent=agent,
            tool_name=tool_name,
            arguments=arguments,
        )
        status = result.decision.value
        reason = result.reason or ""
        policy_id = result.policy_id
        await audit_service.log(
            session,
            event=AuditEvent.POLICY_EVALUATED,
            actor_type=ActorType.SYSTEM,
            actor_id="policy_engine",
            organization_id=connector.organization_id,
            tool=tool_name,
            result=status,
            payload={
                "policy_id": result.policy_id,
                "policy_version": result.policy_version,
                "matched_rule_id": result.matched_rule_id,
                "reason": result.reason,
                "arguments": arguments,
                "agent_id": agent.id,
                "run_id": message.get("run_id"),
                "action_id": message.get("action_id"),
            },
        )
    elif agent is None or agent.organization_id != connector.organization_id:
        reason = "Unknown agent"
    else:
        reason = "Tool is required"
    await websocket.send_json(
        {
            "type": "policy.decision",
            "request_id": request_id,
            "status": status,
            "policy_id": policy_id,
            "reason": reason,
        }
    )


async def _runtime_connect(
    websocket: WebSocket,
    session,
    *,
    runtime_id: str,
    token: str,
    agent_id: str,
):
    await websocket.accept()
    if token and agent_id:
        agent = await _auth_agent(session, agent_id, token)
        if agent is None:
            await websocket.send_json({"type": "error", "error": "unauthorized"})
            await websocket.close(code=4401, reason="unauthorized")
            return
        await _bound_runtime_loop(
            websocket, session, agent, runtime_id=runtime_id, greet=False
        )
        return

    try:
        raw = await websocket.receive_text()
        message = json.loads(raw)
    except (WebSocketDisconnect, json.JSONDecodeError):
        await websocket.close(code=4400, reason="invalid_json")
        return
    if str(message.get("type") or "") != "connection.init":
        await websocket.send_json({"type": "error", "error": "expected_connection.init"})
        await websocket.close(code=4400, reason="expected_connection.init")
        return
    agent = await _auth_agent(
        session,
        str(message.get("agent_id") or agent_id),
        str(message.get("token") or token),
    )
    if agent is None:
        await websocket.send_json({"type": "error", "error": "unauthorized"})
        await websocket.close(code=4401, reason="unauthorized")
        return
    await _bound_runtime_loop(
        websocket,
        session,
        agent,
        runtime_id=str(message.get("runtime_id") or runtime_id),
        greet=False,
        first_message=message,
    )


async def _bound_runtime_loop(
    websocket: WebSocket,
    session,
    agent: Agent,
    *,
    runtime_id: str,
    greet: bool,
    first_message: dict[str, Any] | None = None,
):
    agent.last_seen_at = utcnow()
    await session.flush()
    await _release(session)
    await agent_hub.register_agent(
        agent_id=agent.id,
        organization_id=agent.organization_id,
        websocket=websocket,
    )
    if runtime_id:
        agent_hub.touch(agent.id, runtime_id=runtime_id)
    if greet:
        await websocket.send_json(
            {
                "type": "hello_ok",
                "agent_id": agent.id,
                "organization_id": agent.organization_id,
                "protocol": 2,
            }
        )
    else:
        await websocket.send_json(
            {
                "type": "connection.ack",
                "runtime_id": runtime_id or None,
                "agent_id": agent.id,
                "organization_id": agent.organization_id,
                "protocol": 2,
            }
        )
    if first_message is not None:
        await _handle_agent_message(session, agent, first_message, runtime_id=runtime_id)
        await _release(session)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "error": "invalid_json"})
                continue
            await _handle_agent_message(
                session, agent, message, runtime_id=runtime_id
            )
            await _release(session)
    except WebSocketDisconnect:
        pass
    finally:
        await agent_hub.unregister_agent(agent.id, websocket)


async def _handle_agent_message(
    session, agent: Agent, message: dict[str, Any], *, runtime_id: str = ""
) -> None:
    kind = str(message.get("type") or "")
    runtime_id = str(message.get("runtime_id") or runtime_id)
    heartbeat = kind in {"heartbeat", "heartbeat.ack", "heartbeat_ack"}
    agent_hub.touch(agent.id, heartbeat=heartbeat, runtime_id=runtime_id)
    agent.last_seen_at = utcnow()
    await session.flush()
    if kind == "connection.init":
        await agent_hub.send_agent(
            agent.id,
            {
                "type": "connection.ack",
                "runtime_id": runtime_id or None,
                "agent_id": agent.id,
                "organization_id": agent.organization_id,
                "protocol": 2,
            },
        )
        return
    if kind == "runtime.register":
        replayed = await agent_hub.replay_pending(agent.id)
        await agent_hub.send_agent(
            agent.id,
            {
                "type": "runtime.registered",
                "runtime_id": runtime_id or None,
                "organization_id": agent.organization_id,
                "agent_id": agent.id,
                "replayed": replayed,
            },
        )
        return
    if kind == "ping":
        await agent_hub.send_agent(agent.id, {"type": "pong"})
        return
    if kind in {"heartbeat", "heartbeat_ack"}:
        await agent_hub.send_agent(agent.id, {"type": "heartbeat.ack", "runtime_id": runtime_id or None})
        await agent_hub.broadcast_org(
            agent.organization_id,
            {
                "type": "runtime.heartbeat",
                "agent_id": agent.id,
                "runtime_id": runtime_id or None,
                "last_heartbeat_at": utcnow().isoformat(),
            },
        )
        return
    if kind == "command.status":
        command_id = _command_id(message)
        status = str(message.get("status") or "")
        if status in {"completed", "failed", "cancelled", "interrupted", "unknown"}:
            agent_hub.complete_command(agent.id, command_id)
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        if status in {"completed", "failed", "cancelled"} and payload.get("run_id"):
            await _apply_run_payload(session, agent, payload, command_id)
        return
    if kind in _RUN_EVENTS:
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        if kind != "result" and not payload.get("run_id"):
            return
        await _apply_run_payload(session, agent, payload, _command_id(message))
        return
    if kind in {"event", "tool.requested", "tool.completed"}:
        await agent_hub.broadcast_org(
            agent.organization_id,
            {
                "type": "agent.event",
                "agent_id": agent.id,
                "payload": message,
            },
        )


async def _apply_run_payload(session, agent: Agent, payload: dict[str, Any], command_id: str) -> None:
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        return
    try:
        run = await run_service.load_run(session, run_id)
    except HTTPException:
        return
    if run.agent_id != agent.id:
        return
    await apply_runtime_result(session, run, payload)
    await session.commit()
    agent_hub.complete_command(agent.id, command_id)
    await agent_hub.broadcast_org(
        agent.organization_id,
        {"type": "run.updated", "run": _run_payload(run)},
    )


@router.websocket("/ws/panel")
async def panel_socket(websocket: WebSocket, session: SessionDep, token: str):
    await websocket.accept()
    user = await auth_service.user_from_token(session, token)
    await _release(session)
    if user is None:
        await websocket.send_json({"type": "error", "error": "unauthorized"})
        await websocket.close(code=4401, reason="unauthorized")
        return
    link = await agent_hub.register_panel(
        user_id=user.id,
        organization_id=user.organization_id,
        websocket=websocket,
    )
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "error": "invalid_json"})
                continue
            await _handle_panel_command(session, user, websocket, message)
            await _release(session)
    except WebSocketDisconnect:
        pass
    finally:
        await agent_hub.unregister_panel(link)


async def _handle_panel_command(
    session, user: User, websocket: WebSocket, message: dict[str, Any]
) -> None:
    if str(message.get("type") or "") != "command":
        await agent_hub.send_panel(
            websocket,
            {"type": "error", "error": "unknown_message"},
        )
        return
    command = str(message.get("command") or "")
    agent_id = str(message.get("agent_id") or "")
    request_id = str(message.get("id") or new_id("cmd"))
    employee = await mapping_service.employee_for_user(session, user.id)
    if employee is None or not allows(employee.permissions, "agents", "write"):
        await _reply_panel(
            user.organization_id,
            {"type": "command.error", "id": request_id, "error": "forbidden"},
        )
        return
    if command == "ping":
        delivered = await agent_hub.send_command(agent_id, "ping", {})
        await _reply_panel(
            user.organization_id,
            {
                "type": "command.ack",
                "id": request_id,
                "command": "ping",
                "agent_id": agent_id,
                "delivered": delivered,
            },
        )
        return
    if command == "run":
        prompt = str(message.get("input") or "")
        try:
            run = await run_service.create_run(
                session,
                user,
                agent_id=agent_id,
                prompt=prompt,
                actor_id=user.id,
            )
        except HTTPException as exc:
            await session.rollback()
            await _reply_panel(
                user.organization_id,
                {
                    "type": "command.error",
                    "id": request_id,
                    "error": _error_detail(exc),
                },
            )
            return
        await session.flush()
        await session.commit()
        payload = _run_payload(run)
        await _reply_panel(
            user.organization_id,
            {
                "type": "command.ack",
                "id": request_id,
                "command": "run",
                "agent_id": agent_id,
                "delivered": str(run.status) not in {"failed", "cancelled"},
                "run": payload,
            },
        )
        await agent_hub.broadcast_org(
            user.organization_id,
            {"type": "run.updated", "run": payload},
        )
        return
    await _reply_panel(
        user.organization_id,
        {"type": "command.error", "id": request_id, "error": "unknown_command"},
    )


async def _reply_panel(organization_id: str, message: dict[str, Any]) -> None:
    await agent_hub.broadcast_org(organization_id, message)

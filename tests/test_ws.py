from contextlib import contextmanager

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_session
from app.main import app
from app.models import *  # noqa: F401,F403
from app.services.agent_hub import agent_hub
from tests.catalog import seed_test_catalog


@contextmanager
def ws_client():
    engine = None
    factory = None

    async def override_session():
        nonlocal engine, factory
        if engine is None:
            engine = create_async_engine(
                "sqlite+aiosqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                await seed_test_catalog(session)
                await session.commit()
        async with factory() as session:
            try:
                yield session
            except HTTPException:
                try:
                    await session.commit()
                except Exception:
                    pass
                raise
            except Exception:
                try:
                    await session.rollback()
                except Exception:
                    pass
                raise
            else:
                try:
                    await session.commit()
                except Exception:
                    pass

    app.dependency_overrides[get_session] = override_session
    agent_hub.reset()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        agent_hub.reset()


def _login(client: TestClient) -> str:
    response = client.post(
        "/auth/login",
        json={"email": "rick@saturn.ai", "password": "saturn"},
    )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return token


def test_enroll_issues_token_and_agent_hello():
    with ws_client() as client:
        _login(client)
        listed = client.get("/agents").json()
        seed = next(item for item in listed if item["id"] == "agent_customer_ops")
        assert seed["connected"] is False
        assert seed["enrolled"] is False
        assert "token_hash" not in seed
        assert client.get("/agents/agent_customer_ops/enrollment").status_code == 404

        enroll = client.post("/agents/agent_customer_ops/enroll")
        assert enroll.status_code == 200, enroll.text
        body = enroll.json()
        assert body["agent_id"] == "agent_customer_ops"
        assert body["token"].startswith("agt_")
        assert "token=" in body["ws_url"]

        after = client.get("/agents/agent_customer_ops").json()
        assert after["connected"] is False
        assert after["enrolled"] is True
        assert "token" not in after
        assert "token_hash" not in after

        saved = client.get("/agents/agent_customer_ops/enrollment")
        assert saved.status_code == 200, saved.text
        assert saved.json()["token"] == body["token"]
        assert saved.json()["ws_url"] == body["ws_url"]

        rotated = client.post("/agents/agent_customer_ops/enroll").json()
        assert rotated["token"] != body["token"]
        assert client.get("/agents/agent_customer_ops/enrollment").json()["token"] == rotated["token"]

        with client.websocket_connect(
            f"/ws/agents?agent_id=agent_customer_ops&token={rotated['token']}"
        ) as agent_ws:
            hello = agent_ws.receive_json()
            assert hello["type"] == "hello_ok"
            assert hello["agent_id"] == "agent_customer_ops"
            live = client.get("/agents/agent_customer_ops").json()
            assert live["connected"] is True


def test_agent_socket_rejects_bad_token():
    with ws_client() as client:
        _login(client)
        client.post("/agents/agent_customer_ops/enroll")
        with client.websocket_connect(
            "/ws/agents?agent_id=agent_customer_ops&token=agt_invalid"
        ) as agent_ws:
            message = agent_ws.receive_json()
            assert message["type"] == "error"
            assert message["error"] == "unauthorized"


def test_panel_snapshot_and_commands():
    with ws_client() as client:
        token = _login(client)
        enroll = client.post("/agents/agent_customer_ops/enroll").json()
        with client.websocket_connect(
            f"/ws/agents?agent_id=agent_customer_ops&token={enroll['token']}"
        ) as agent_ws:
            assert agent_ws.receive_json()["type"] == "hello_ok"
            with client.websocket_connect(f"/ws/panel?token={token}") as panel_ws:
                snapshot = panel_ws.receive_json()
                assert snapshot["type"] == "snapshot"
                assert any(
                    item["id"] == "agent_customer_ops" and item["connected"]
                    for item in snapshot["agents"]
                )

                panel_ws.send_json(
                    {
                        "type": "command",
                        "command": "ping",
                        "id": "cmd_ping",
                        "agent_id": "agent_customer_ops",
                    }
                )
                ping_cmd = agent_ws.receive_json()
                assert ping_cmd["type"] == "command"
                assert ping_cmd["command"] == "ping"
                ack = panel_ws.receive_json()
                assert ack["type"] == "command.ack"
                assert ack["command"] == "ping"
                assert ack["delivered"] is True

                panel_ws.send_json(
                    {
                        "type": "command",
                        "command": "run",
                        "id": "cmd_run",
                        "agent_id": "agent_customer_ops",
                        "input": "Refund Acme's duplicate charge.",
                    }
                )
                run_cmd = agent_ws.receive_json()
                assert run_cmd["command"] == "run"
                assert run_cmd["command_id"]
                assert run_cmd["payload"]["run_id"]
                assert "billing.refund" in run_cmd["payload"]["tools"]
                run_ack = panel_ws.receive_json()
                assert run_ack["type"] == "command.ack"
                assert run_ack["command"] == "run"
                assert run_ack["run"]["status"] == "running"
                updated = panel_ws.receive_json()
                assert updated["type"] == "run.updated"
                assert updated["run"]["id"] == run_ack["run"]["id"]

                agent_ws.send_json(
                    {
                        "type": "result",
                        "id": run_cmd["id"],
                        "command": "run",
                        "payload": {
                            "run_id": run_cmd["payload"]["run_id"],
                            "status": "waiting_approval",
                            "approval_id": "approval_live",
                            "actions": [
                                {
                                    "id": "action_live_1",
                                    "tool": "crm.get_customer",
                                    "arguments": {"name": "Acme"},
                                    "policy_decision": "allow",
                                    "status": "completed",
                                    "result": {"name": "Acme"},
                                },
                                {
                                    "id": "action_live_2",
                                    "tool": "billing.refund",
                                    "arguments": {"amount": 840, "name": "Acme"},
                                    "policy_decision": "approval_required",
                                    "status": "waiting_approval",
                                    "result": None,
                                },
                            ],
                        },
                    }
                )
                applied = panel_ws.receive_json()
                assert applied["type"] == "run.updated"
                assert applied["run"]["status"] == "waiting_approval"
                assert applied["run"]["approval_id"] == "approval_live"


def test_http_run_works_without_live_agent():
    with ws_client() as client:
        _login(client)
        created = client.post(
            "/runs",
            json={
                "agent_id": "agent_customer_ops",
                "input": "Refund Acme's duplicate charge.",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "waiting_approval"


def test_panel_rejects_bad_session():
    with ws_client() as client:
        with client.websocket_connect("/ws/panel?token=sat_invalid") as panel_ws:
            message = panel_ws.receive_json()
            assert message["type"] == "error"
            assert message["error"] == "unauthorized"


def test_runtime_protocol_ack_heartbeat_and_org_binding():
    with ws_client() as client:
        _login(client)
        enroll = client.post("/agents/agent_customer_ops/enroll").json()
        with client.websocket_connect(
            f"/ws/runtime?runtime_id=rt_acme_001&agent_id=agent_customer_ops&token={enroll['token']}"
        ) as runtime_ws:
            ack = runtime_ws.receive_json()
            assert ack["type"] == "connection.ack"
            assert ack["organization_id"]
            assert ack["agent_id"] == "agent_customer_ops"
            runtime_ws.send_json(
                {
                    "type": "runtime.register",
                    "runtime_id": "rt_acme_001",
                    "agents": ["agent_customer_ops"],
                }
            )
            registered = runtime_ws.receive_json()
            assert registered["type"] == "runtime.registered"
            assert registered["organization_id"] == ack["organization_id"]
            runtime_ws.send_json({"type": "heartbeat", "runtime_id": "rt_acme_001"})
            beat = runtime_ws.receive_json()
            assert beat["type"] == "heartbeat.ack"
            live = client.get("/agents/agent_customer_ops").json()
            assert live["connected"] is True
            assert live["last_seen_at"]


def test_agent_socket_still_accepts_legacy_hello():
    with ws_client() as client:
        _login(client)
        enroll = client.post("/agents/agent_customer_ops/enroll").json()
        with client.websocket_connect(
            f"/ws/agents?agent_id=agent_customer_ops&token={enroll['token']}"
        ) as agent_ws:
            hello = agent_ws.receive_json()
            assert hello["type"] == "hello_ok"
            agent_ws.send_json({"type": "connection.init", "runtime_id": "rt_acme_001"})
            ack = agent_ws.receive_json()
            assert ack["type"] == "connection.ack"
            assert ack["organization_id"] == hello["organization_id"]


def test_connector_enroll_registers_agents_mcp_and_routes_commands():
    with ws_client() as client:
        token = _login(client)
        before = client.get("/connectors").json()
        assert before["enrolled"] is False
        assert before["connected"] is False
        assert client.get("/connectors/enrollment").status_code == 404

        enroll = client.post("/connectors/enroll")
        assert enroll.status_code == 200, enroll.text
        body = enroll.json()
        assert body["connector_id"].startswith("conn_")
        assert body["token"].startswith("conn_")
        assert "token=" in body["ws_url"]

        saved = client.get("/connectors/enrollment").json()
        assert saved["token"] == body["token"]
        listed = client.get("/connectors").json()
        assert listed["enrolled"] is True
        assert listed["connected"] is False

        with client.websocket_connect(
            f"/ws/connectors?connector_id={body['connector_id']}&token={body['token']}"
        ) as connector_ws:
            ack = connector_ws.receive_json()
            assert ack["type"] == "connection.ack"
            assert ack["organization_id"]
            registered = connector_ws.receive_json()
            assert registered["type"] == "connector.registered"
            live = client.get("/connectors").json()
            assert live["connected"] is True

            connector_ws.send_json(
                {
                    "type": "runtime.register",
                    "runtime_id": "runtime_finance",
                    "name": "Finance Runtime",
                    "agents": ["agent_billing"],
                }
            )
            runtime_ok = connector_ws.receive_json()
            assert runtime_ok["type"] == "runtime.registered"
            assert "agent_billing" in runtime_ok["agents"]

            agent = client.get("/agents/agent_billing").json()
            assert agent["connected"] is True
            assert agent["connector_id"] == body["connector_id"]
            assert agent["runtime_id"] == "runtime_finance"

            connector_ws.send_json(
                {
                    "type": "mcp.register",
                    "server_id": "acme-enterprise-mcp",
                    "name": "ACME Enterprise MCP",
                    "url": "http://mcp:4100/mcp",
                    "tools": ["crm.get_customer", "ops.sync_ledger"],
                }
            )
            mcp_ok = connector_ws.receive_json()
            assert mcp_ok["type"] == "mcp.registered"
            servers = client.get("/mcp-servers").json()
            assert any(item["id"] == "acme-enterprise-mcp" for item in servers)
            tools = client.get("/tools").json()
            assert any(item["name"] == "ops.sync_ledger" for item in tools)

            with client.websocket_connect(f"/ws/panel?token={token}") as panel_ws:
                snapshot = panel_ws.receive_json()
                assert snapshot["type"] == "snapshot"
                assert snapshot["connector"]["connected"] is True
                assert any(
                    item["id"] == "agent_billing" and item["connected"]
                    for item in snapshot["agents"]
                )
                panel_ws.send_json(
                    {
                        "type": "command",
                        "command": "run",
                        "id": "cmd_conn_run",
                        "agent_id": "agent_billing",
                        "input": "Sync last week's ledger.",
                    }
                )
                command = connector_ws.receive_json()
                assert command["type"] == "command"
                assert command["command"] == "run"
                assert command["agent_id"] == "agent_billing"
                assert command["runtime_id"] == "runtime_finance"
                assert command["payload"]["run_id"]
                ack_cmd = panel_ws.receive_json()
                assert ack_cmd["type"] == "command.ack"
                assert ack_cmd["command"] == "run"
                assert ack_cmd["delivered"] is True
                assert ack_cmd["run"]["agent_id"] == "agent_billing"


def test_connector_socket_rejects_bad_token():
    with ws_client() as client:
        _login(client)
        client.post("/connectors/enroll")
        with client.websocket_connect(
            "/ws/connectors?connector_id=conn_x&token=conn_invalid"
        ) as connector_ws:
            message = connector_ws.receive_json()
            assert message["type"] == "error"
            assert message["error"] == "unauthorized"

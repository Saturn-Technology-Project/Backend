"""Each request gets an id and one log line, and the line never carries the query string."""

import json
import logging

import pytest

from app.core import logs
from app.core.logs import JsonFormatter, RequestLogMiddleware, configure_logging, request_id_var


@pytest.fixture
def request_lines(caplog):
    caplog.set_level(logging.INFO, logger="saturn.request")
    return lambda: [record for record in caplog.records if record.name == "saturn.request"]


async def test_every_response_names_its_request(client, request_lines):
    first = await client.get("/agents")
    second = await client.get("/agents")
    assert len(first.headers["x-request-id"]) == 16
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    lines = request_lines()
    assert [(line.http_method, line.http_path, line.http_status) for line in lines] == [("GET", "/agents", 200)] * 2
    assert all(line.duration_ms >= 0 for line in lines)


async def test_a_caller_cannot_choose_the_request_id(client):
    response = await client.get("/agents", headers={"X-Request-Id": "chosen-by-caller"})
    assert response.headers["x-request-id"] != "chosen-by-caller"


async def test_the_query_string_is_not_logged(client, request_lines):
    await client.get("/oauth/authorize", params={"client_id": "c", "state": "secret-state-value", "code": "secret-code"})
    line = request_lines()[-1]
    assert line.http_path == "/oauth/authorize"
    assert "secret" not in line.getMessage() and "secret" not in json.dumps(vars(line), default=str)


async def test_probes_are_not_logged_and_refusals_are(client, request_lines, monkeypatch):
    await client.get("/healthz")
    assert request_lines() == []
    # A request the origin check refuses still gets an id and a line.
    monkeypatch.setenv("ORIGIN_AUTH_SECRET", "edge-secret")
    refused = await client.get("/agents")
    assert refused.status_code == 403 and refused.headers["x-request-id"]
    assert [(line.http_path, line.http_status) for line in request_lines()] == [("/agents", 403)]


async def test_an_unhandled_error_is_logged_with_its_request_id(caplog):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async def boom():
        raise RuntimeError("the cause, which the caller must not see")

    app.add_api_route("/__boom", boom)
    try:
        caplog.set_level(logging.ERROR)
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as raw:
            response = await raw.get("/__boom")
    finally:
        app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", "") != "/__boom"]
    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Internal server error" and "cause" not in response.text
    assert body["request_id"] == response.headers["x-request-id"]
    assert response.headers["x-content-type-options"] == "nosniff"
    failure = next(record for record in caplog.records if record.name == "saturn.error")
    assert failure.exc_info is not None and "RuntimeError" in str(failure.exc_info[0])


def test_json_lines_carry_severity_request_id_and_extras():
    token = request_id_var.set("abc123")
    try:
        record = logging.LogRecord("saturn.test", logging.WARNING, __file__, 1, "slow %s", ("query",), None)
        record.duration_ms = 12.5
        entry = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert entry["severity"] == "WARNING" and entry["message"] == "slow query"
    assert entry["request_id"] == "abc123" and entry["duration_ms"] == 12.5
    assert entry["logger"] == "saturn.test" and entry["time"].endswith("+00:00")


def test_logging_is_json_on_cloud_run_and_text_elsewhere(monkeypatch):
    root = logging.getLogger()
    before = list(root.handlers), root.level
    try:
        monkeypatch.delenv("K_SERVICE", raising=False)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        configure_logging()
        ours = [handler for handler in root.handlers if getattr(handler, "_saturn", False)]
        assert len(ours) == 1 and isinstance(ours[0].formatter, logs.TextFormatter)
        monkeypatch.setenv("K_SERVICE", "saturn-backend")
        configure_logging()
        ours = [handler for handler in root.handlers if getattr(handler, "_saturn", False)]
        # Calling it again replaces the handler instead of adding a second one.
        assert len(ours) == 1 and isinstance(ours[0].formatter, JsonFormatter)
        assert logging.getLogger("uvicorn.access").propagate is False
    finally:
        root.handlers[:] = before[0]
        root.setLevel(before[1])


def test_the_middleware_is_the_outermost_layer():
    from app.main import app

    assert app.user_middleware[0].cls is RequestLogMiddleware

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import HTTPException, status

from app.features.mcp.servers.services.mcp_endpoint import require_mcp_endpoint


def sec_api_request(endpoint: str, authorization: str) -> tuple[str, str]:
    """SEC-API.io reads the key from apiKey, not from Authorization."""
    url = endpoint.strip()
    token = authorization.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    parts = urlsplit(url)
    if parts.hostname != "api.sec-api.io" or not token:
        return url, authorization
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if query.get("apiKey"):
        return url, ""
    query["apiKey"] = token
    signed = urlunsplit((parts.scheme, parts.netloc, parts.path or "/mcp", urlencode(query), ""))
    return signed, ""


def _headers(authorization: str, session_id: str = "") -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    token = authorization.strip()
    if token:
        headers["authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
    if session_id:
        headers["mcp-session-id"] = session_id
    return headers


def _payload(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
                return parsed
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "MCP server returned an empty stream")
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "MCP server did not return JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "MCP server did not return JSON")
    return body


def _tools(body: dict[str, Any]) -> list[dict[str, Any]]:
    if body.get("error"):
        message = body["error"].get("message") if isinstance(body["error"], dict) else "MCP tools/list failed"
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(message or "MCP tools/list failed"))
    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    raw = result.get("tools") if isinstance(result.get("tools"), list) else []
    tools: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
            continue
        schema = item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {"type": "object", "properties": {}}
        tools.append(
            {
                "name": item["name"].strip(),
                "description": str(item.get("description") or ""),
                "inputSchema": schema,
            }
        )
    return tools


class McpUnauthorized(Exception):
    def __init__(self, www_authenticate: str, endpoint: str) -> None:
        self.www_authenticate = www_authenticate
        self.endpoint = endpoint


def _unauthorized(response: httpx.Response, endpoint: str) -> None:
    if response.status_code == 401:
        raise McpUnauthorized(response.headers.get("www-authenticate", ""), endpoint)


def _raise_for_status(response: httpx.Response, endpoint: str) -> None:
    _unauthorized(response, endpoint)
    if response.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"MCP server returned HTTP {response.status_code}")


async def _rpc(
    client: httpx.AsyncClient,
    endpoint: str,
    headers: dict[str, str],
    method: str,
    params: dict[str, Any],
    request_id: int,
) -> httpx.Response:
    try:
        return await client.post(
            endpoint,
            headers=headers,
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not reach MCP server: {exc}") from exc


async def list_remote_tools(endpoint: str, authorization: str = "") -> list[dict[str, Any]]:
    url = require_mcp_endpoint(endpoint)
    request_url, header_auth = sec_api_request(url, authorization)
    headers = _headers(header_auth)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        init = await _rpc(
            client,
            request_url,
            headers,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "Saturn", "version": "0.1.0"},
            },
            1,
        )
        _raise_for_status(init, url)
        session_id = init.headers.get("mcp-session-id", "")
        if session_id:
            headers = _headers(header_auth, session_id)
        listed = await _rpc(client, request_url, headers, "tools/list", {}, 2)
    _raise_for_status(listed, url)
    tools = _tools(_payload(listed))
    if not tools:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "MCP server did not list any tools")
    return tools


async def call_remote_tool(
    endpoint: str,
    name: str,
    arguments: dict[str, Any],
    authorization: str = "",
) -> dict[str, Any]:
    url = require_mcp_endpoint(endpoint)
    request_url, header_auth = sec_api_request(url, authorization)
    headers = _headers(header_auth)
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        init = await _rpc(
            client,
            request_url,
            headers,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "Saturn", "version": "0.1.0"},
            },
            1,
        )
        _raise_for_status(init, url)
        session_id = init.headers.get("mcp-session-id", "")
        if session_id:
            headers = _headers(header_auth, session_id)
        called = await _rpc(
            client,
            request_url,
            headers,
            "tools/call",
            {"name": name, "arguments": arguments},
            2,
        )
    _raise_for_status(called, url)
    body = _payload(called)
    if body.get("error"):
        message = body["error"].get("message") if isinstance(body["error"], dict) else "MCP tools/call failed"
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(message or "MCP tools/call failed"))
    return body

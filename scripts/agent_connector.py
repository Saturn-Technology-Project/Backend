#!/usr/bin/env python3
"""Dial the Saturn control plane as an Agent runtime.

Keeps a logical connection: if the WebSocket drops, reconnect with
exponential backoff and jitter. Heartbeats keep the session alive.
Command ids are remembered so a replay after reconnect is not executed twice.

Prefer the TypeScript Saturn Agent Runtime for MCP execution:

    SATURN_CLOUD_WS=ws://localhost:8000/ws/agents \\
    SATURN_AGENT_ID=<agent_id> \\
    SATURN_AGENT_TOKEN=agt_... \\
    npm start
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time

from websockets.asyncio.client import connect

BACKOFF_SEC = (1, 2, 4, 8, 16, 30, 60)
HEARTBEAT_SEC = 25


def reconnect_delay(attempt: int) -> float:
    base = BACKOFF_SEC[min(max(attempt, 0), len(BACKOFF_SEC) - 1)]
    jitter = base * (random.random() * 0.4 - 0.2)
    return max(0.25, base + jitter)


def command_id_of(message: dict) -> str:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    return str(message.get("command_id") or payload.get("command_id") or message.get("id") or "")


def command_name_of(message: dict) -> str:
    command = str(message.get("command") or "")
    if command:
        return command
    kind = str(message.get("type") or "")
    if kind.startswith("command."):
        return kind[len("command.") :]
    return kind


async def session(url: str, agent_id: str, token: str, processed: dict[str, dict]) -> None:
    sep = "&" if "?" in url else "?"
    uri = f"{url}{sep}agent_id={agent_id}&token={token}"
    async with connect(uri) as socket:
        print("connected", file=sys.stderr)
        await socket.send(
            json.dumps(
                {
                    "type": "connection.init",
                    "agent_id": agent_id,
                    "token": token,
                    "protocol": 2,
                }
            )
        )
        await socket.send(json.dumps({"type": "runtime.register", "agents": [agent_id]}))

        async def heartbeats() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_SEC)
                await socket.send(json.dumps({"type": "heartbeat"}))

        beat = asyncio.create_task(heartbeats())
        try:
            async for raw in socket:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    print(raw)
                    continue
                print(json.dumps(message, ensure_ascii=False))
                kind = str(message.get("type") or "")
                if kind in {"hello_ok", "connection.ack", "runtime.registered", "heartbeat.ack"}:
                    continue
                if kind != "command" and not kind.startswith("command."):
                    continue
                cmd_id = command_id_of(message)
                command = command_name_of(message)
                if cmd_id in processed:
                    await socket.send(json.dumps(processed[cmd_id]))
                    continue
                if command == "ping":
                    reply = {"type": "pong", "id": cmd_id, "command_id": cmd_id}
                else:
                    reply = {
                        "type": "event",
                        "id": cmd_id,
                        "command_id": cmd_id,
                        "command": command,
                        "payload": message.get("payload"),
                    }
                processed[cmd_id] = reply
                await socket.send(json.dumps(reply))
        finally:
            beat.cancel()


async def run(url: str, agent_id: str, token: str) -> None:
    attempt = 0
    processed: dict[str, dict] = {}
    while True:
        started = time.monotonic()
        try:
            await session(url, agent_id, token, processed)
            attempt = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"disconnected: {exc}", file=sys.stderr)
            if time.monotonic() - started > 30:
                attempt = 0
        delay = reconnect_delay(attempt)
        attempt += 1
        print(f"reconnect in {delay:.1f}s", file=sys.stderr)
        await asyncio.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://localhost:8000/ws/agents")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.url, args.agent_id, args.token))


if __name__ == "__main__":
    main()

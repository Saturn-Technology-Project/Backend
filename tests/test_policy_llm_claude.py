"""The policy model is Claude, called through the Anthropic SDK against a mocked Messages API."""

from __future__ import annotations

import json

import httpx2
import pytest
from anthropic import AsyncAnthropic
from fastapi import HTTPException

from app.core.config import Settings
from app.features.policy.policies.services import policy_llm


def _sse(events: list[dict]) -> bytes:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


def _stream(thinking: str, text: str, stop_reason: str = "end_turn") -> bytes:
    usage = {"input_tokens": 10, "output_tokens": 5}
    return _sse(
        [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-5-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": usage,
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": thinking}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": usage},
            {"type": "message_stop"},
        ]
    )


@pytest.fixture
def claude(monkeypatch):
    """Point the policy model at a mocked Messages API and record each request body."""
    seen: list[dict] = []
    reply = {"body": b""}

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/messages"
        seen.append(json.loads(request.content))
        return httpx2.Response(200, content=reply["body"], headers={"content-type": "text/event-stream"})

    settings = Settings(policy_llm_api_key="test-key")
    monkeypatch.setattr(policy_llm, "get_settings", lambda: settings)
    monkeypatch.setattr(
        policy_llm,
        "_client",
        lambda: AsyncAnthropic(api_key="test-key", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))),
    )
    return seen, reply


_MESSAGES = [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "{}"}]


async def test_stream_yields_thinking_and_parses_the_json(claude):
    seen, reply = claude
    reply["body"] = _stream("Checking the tools.", '{"tools": []}')

    deltas = [item async for item in policy_llm._iter_deltas(_MESSAGES)]
    assert deltas == [("reasoning", "Checking the tools."), ("content", '{"tools": []}')]

    body = seen[0]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"] == "Return JSON."
    assert body["messages"] == [{"role": "user", "content": "{}"}]
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "temperature" not in body

    results = [item async for item in policy_llm.stream_chat(_MESSAGES)]
    assert results == [("result", {"tools": []})]


async def test_chat_reads_the_final_message(claude):
    _, reply = claude
    reply["body"] = _stream("Thinking.", 'Here it is: {"tools": [{"tool": "a"}]}')
    assert await policy_llm._chat(_MESSAGES) == {"tools": [{"tool": "a"}]}


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
async def test_a_refused_or_cut_off_reply_is_a_bad_gateway(claude, stop_reason):
    _, reply = claude
    reply["body"] = _stream("", '{"tools": ', stop_reason=stop_reason)
    with pytest.raises(HTTPException) as caught:
        await policy_llm._chat(_MESSAGES)
    assert caught.value.status_code == 502


async def test_no_api_key_means_no_policy_model(monkeypatch):
    monkeypatch.setattr(policy_llm, "get_settings", lambda: Settings(policy_llm_api_key=""))
    assert not policy_llm._configured()
    analyses, used = await policy_llm.analyze_tools([{"name": "a", "description": "d"}])
    assert used is False
    assert analyses[0]["status"] == "insufficient"

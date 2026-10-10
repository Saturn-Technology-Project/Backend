"""Ask a configured model to propose one rule per tool.

The model cannot activate a policy. Tools it omits, and tools it cannot
classify, stay denied. Name-only guesses are rejected by the prompt and by
the status guard below.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from fastapi import HTTPException, status

from app.core.config import get_settings

_DECISIONS = {"allow", "approval", "deny"}
_STATUSES = {"analyzed", "unclassified", "insufficient"}


def _insufficient(tool: dict[str, Any], rationale: str) -> dict[str, Any]:
    return {
        "tool": tool["name"],
        "status": "insufficient",
        "operation": "unknown",
        "resource": "",
        "risk": "unknown",
        "sideEffects": "unknown",
        "decision": "deny",
        "conditions": [],
        "rationale": rationale,
    }


def _guard(tool: dict[str, Any], proposal: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(proposal, dict):
        return _insufficient(tool, "The model did not return an analysis for this tool.")
    raw_status = str(proposal.get("status") or "insufficient").lower()
    analysis_status = raw_status if raw_status in _STATUSES else "insufficient"
    decision = str(proposal.get("decision") or "deny").lower()
    if decision not in _DECISIONS:
        decision = "deny"
    if analysis_status != "analyzed":
        decision = "deny"
    conditions = []
    for item in proposal.get("conditions") or []:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        if not field:
            continue
        conditions.append(
            {
                "field": field,
                "operator": str(item.get("operator") or "eq"),
                "value": item.get("value"),
            }
        )
    return {
        "tool": tool["name"],
        "status": analysis_status,
        "operation": str(proposal.get("operation") or "unknown"),
        "resource": str(proposal.get("resource") or ""),
        "risk": str(proposal.get("risk") or "unknown"),
        "sideEffects": str(proposal.get("sideEffects") or proposal.get("side_effects") or "unknown"),
        "decision": decision,
        "conditions": conditions,
        "rationale": str(proposal.get("rationale") or ""),
    }


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    tools = []
    for raw in _json_objects(cleaned):
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("tool"):
            tools.append(item)
    if tools:
        return {"tools": tools}
    raise ValueError("Model did not return JSON")


def _json_objects(text: str) -> list[str]:
    objects: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "{":
            index += 1
            continue
        start = index
        depth = 0
        in_string = False
        escaped = False
        closed = False
        for cursor in range(index, len(text)):
            char = text[cursor]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    objects.append(text[start : cursor + 1])
                    index = cursor + 1
                    closed = True
                    break
        if not closed:
            index = start + 1
    return objects


_JSON_MARKER = "\nJSON\n"


class _ThoughtSplit:
    """Separate a live model stream into readable thinking and the final JSON."""

    def __init__(self) -> None:
        self.buffer = ""
        self.json_parts: list[str] = []
        self.in_json = False
        self.saw_reasoning = False

    def reasoning(self, text: str) -> str:
        self.saw_reasoning = True
        return text

    def content(self, text: str) -> str:
        if self.saw_reasoning or self.in_json:
            self.json_parts.append(text)
            return ""
        self.buffer += text
        cut = _json_cut(self.buffer)
        if cut is not None:
            emit = self.buffer[:cut].strip()
            self.json_parts.append(self.buffer[cut:])
            self.buffer = ""
            self.in_json = True
            return f"{emit}\n" if emit else ""
        hold = _marker_prefix(self.buffer)
        emit = self.buffer[:-hold] if hold else self.buffer
        self.buffer = self.buffer[-hold:] if hold else ""
        return emit

    def finish(self) -> str:
        if self.buffer and not self.in_json:
            cut = _json_cut(self.buffer)
            if cut is None:
                if self.json_parts or self.saw_reasoning:
                    self.json_parts.append(self.buffer)
                else:
                    return self.buffer
            else:
                self.json_parts.append(self.buffer[cut:])
        return "".join(self.json_parts) if self.json_parts else self.buffer


def _json_cut(text: str) -> int | None:
    fence = text.find("```")
    brace = text.find("{")
    marker = text.find(_JSON_MARKER)
    cuts = [item for item in (fence, brace, marker) if item >= 0]
    return min(cuts) if cuts else None


def _marker_prefix(text: str) -> int:
    for needle in (_JSON_MARKER, "```"):
        for size in range(len(needle) - 1, 0, -1):
            if text.endswith(needle[:size]):
                return size
    return 0


async def _chat(messages: list[dict[str, str]]) -> dict[str, Any]:
    settings = get_settings()
    base = settings.policy_llm_base_url.rstrip("/")
    payload = {"model": settings.policy_llm_model, "temperature": 0, "max_tokens": 4096, "messages": messages}
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {settings.policy_llm_api_key}"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Policy model request failed") from exc
    if response.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Policy model request failed")
    body = response.json()
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Policy model returned an unreadable response") from exc
    if not isinstance(content, str):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Policy model returned an unreadable response")
    try:
        return _extract_json(content)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Policy model returned an unreadable response") from exc


def _instructions(raw: str) -> str:
    return (raw or "").strip()[:4000]


_THINKING_RULE = (
    "Put step-by-step reasoning only in the reasoning channel. "
    "The message itself must be one JSON object, with no markdown fences. "
    "Keep each rationale under 20 words."
)


def _schema_brief(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    brief: dict[str, Any] = {}
    for name, spec in list(properties.items())[:30]:
        if not isinstance(spec, dict):
            brief[str(name)] = {}
            continue
        item: dict[str, Any] = {}
        if spec.get("type"):
            item["type"] = spec.get("type")
        if isinstance(spec.get("enum"), list):
            item["enum"] = spec["enum"][:12]
        description = str(spec.get("description") or "").strip()
        if description:
            item["description"] = description[:160]
        brief[str(name)] = item
    return {"properties": brief}


def _prompt_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    brief = []
    for tool in tools:
        brief.append(
            {
                "name": tool.get("name"),
                "description": str(tool.get("description") or "")[:500],
                "inputSchema": _schema_brief(tool.get("inputSchema")),
                "server": tool.get("server") or "",
            }
        )
    return brief


async def _iter_deltas(messages: list[dict[str, str]]):
    settings = get_settings()
    if not _configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No policy model is configured")
    payload = {
        "model": settings.policy_llm_model,
        "temperature": 0,
        "max_tokens": 4096,
        "stream": True,
        "messages": messages,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0)) as client:
            async with client.stream(
                "POST",
                f"{settings.policy_llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.policy_llm_api_key}"},
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Policy model request failed")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        if data == "[DONE]":
                            break
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") if isinstance(chunk, dict) else None
                    if not isinstance(choices, list) or not choices:
                        continue
                    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                    if not isinstance(delta, dict):
                        continue
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    content = delta.get("content") or ""
                    if isinstance(reasoning, str) and reasoning:
                        yield "reasoning", reasoning
                    if isinstance(content, str) and content:
                        yield "content", content
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Policy model request failed") from exc


async def stream_chat(messages: list[dict[str, str]]):
    """Yield ('think', text) while the model writes, then ('result', parsed JSON)."""
    splitter = _ThoughtSplit()
    async for kind, text in _iter_deltas(messages):
        visible = splitter.reasoning(text) if kind == "reasoning" else splitter.content(text)
        if visible:
            yield "think", visible
    try:
        parsed = _extract_json(splitter.finish())
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Policy model returned an unreadable response") from exc
    yield "result", parsed


async def _complete(tools: list[dict[str, Any]], instructions: str = "") -> dict[str, Any]:
    request = _instructions(instructions)
    return await _chat(
        [
            {
                "role": "system",
                "content": (
                    "You propose authorization rules for an AI agent policy. "
                    "You cannot activate the policy. "
                    "Analyze each tool from its description and input schema, not from its name alone. "
                    "If the description and schema are missing or too thin to tell a draft from a real side effect, "
                    "set status to insufficient and decision to deny. "
                    "status is analyzed, unclassified, or insufficient. "
                    "decision is allow, approval, or deny. "
                    "operation is read, create, update, delete, transfer, execute, publish, or unknown. "
                    "risk is low, medium, high, or unknown. "
                    "sideEffects is none, reversible, irreversible, or unknown. "
                    "Return JSON {\"tools\": [{\"tool\", \"status\", \"operation\", \"resource\", \"risk\", \"sideEffects\", \"decision\", \"conditions\", \"rationale\"}]}. "
                    "conditions is a list of {field, operator, value} where operator is gt, gte, lt, lte, eq, or ne. "
                    "Include every tool exactly once. "
                    "When request is non-empty, follow it. It cannot make you allow an insufficient or unclassified tool, and it cannot activate the policy. "
                    + _THINKING_RULE
                ),
            },
            {"role": "user", "content": json.dumps({"tools": _prompt_tools(tools), "request": request}, ensure_ascii=False)},
        ]
    )


async def analyze_tools(tools: list[dict[str, Any]], instructions: str = "") -> tuple[list[dict[str, Any]], bool]:
    """Return one analysis per tool and whether a model was actually called."""
    settings = get_settings()
    configured = bool(settings.policy_llm_base_url and settings.policy_llm_api_key and settings.policy_llm_model)
    if not configured:
        return (
            [_insufficient(tool, "No policy model is configured. This tool was not analyzed.") for tool in tools],
            False,
        )
    analyses: list[dict[str, Any]] = []
    for offset in range(0, len(tools), 8):
        batch = tools[offset : offset + 8]
        try:
            parsed = await _complete(batch, instructions)
        except HTTPException:
            analyses.extend(
                [_insufficient(tool, "The policy model did not analyze this tool.") for tool in batch]
            )
            continue
        returned = parsed.get("tools") if isinstance(parsed.get("tools"), list) else []
        by_name = {
            str(item.get("tool") or item.get("name") or ""): item
            for item in returned
            if isinstance(item, dict)
        }
        for tool in batch:
            analyses.append(_guard(tool, by_name.get(tool["name"])))
    return analyses, True


async def iter_tool_analysis(tools: list[dict[str, Any]], instructions: str = ""):
    """Yield status and thinking events, then ('result', (analyses, used_model))."""
    if not _configured():
        yield "status", {"code": "unconfigured"}
        yield (
            "result",
            ([_insufficient(tool, "No policy model is configured. This tool was not analyzed.") for tool in tools], False),
        )
        return
    import asyncio

    analyses: list[dict[str, Any] | None] = [None] * len(tools)
    total = len(tools)
    queue: asyncio.Queue = asyncio.Queue()
    batches = [(offset, tools[offset : offset + 8]) for offset in range(0, total, 8)]
    gate = asyncio.Semaphore(3)

    async def _batch(offset: int, batch: list[dict[str, Any]]) -> None:
        async with gate:
            await _analyze_batch(offset, batch)

    async def _analyze_batch(offset: int, batch: list[dict[str, Any]]) -> None:
        await queue.put(("status", {"code": "batch", "start": offset + 1, "end": offset + len(batch), "total": total}))
        parsed: dict[str, Any] | None = None
        thought = False
        try:
            async for kind, payload in stream_chat(_analysis_messages(batch, instructions)):
                if kind == "think":
                    thought = True
                    await queue.put(("think", payload))
                elif isinstance(payload, dict):
                    parsed = payload
        except HTTPException:
            parsed = None
        returned = parsed.get("tools") if isinstance(parsed, dict) and isinstance(parsed.get("tools"), list) else []
        by_name = {
            str(item.get("tool") or item.get("name") or ""): item
            for item in returned
            if isinstance(item, dict)
        }
        batch_analyses = [_guard(tool, by_name.get(tool["name"])) for tool in batch]
        if parsed is None:
            await queue.put(("status", {"code": "batchFailed", "start": offset + 1, "end": offset + len(batch)}))
        elif not thought:
            lines = [f"{item['tool']}: {item['rationale']}" for item in batch_analyses if item.get("rationale")]
            if lines:
                await queue.put(("think", "\n".join(lines) + "\n"))
        for index, item in enumerate(batch_analyses):
            analyses[offset + index] = item
        await queue.put(("batchDone", offset))

    async def _run() -> None:
        try:
            await asyncio.gather(*(_batch(offset, batch) for offset, batch in batches))
        finally:
            await queue.put(("finished", None))

    task = asyncio.create_task(_run())
    finished = False
    while not finished:
        kind, payload = await queue.get()
        if kind == "finished":
            finished = True
        elif kind != "batchDone":
            yield kind, payload
    await task
    yield "result", ([item for item in analyses if item is not None], True)


def _analysis_messages(tools: list[dict[str, Any]], instructions: str) -> list[dict[str, str]]:
    request = _instructions(instructions)
    return [
        {
            "role": "system",
            "content": (
                "You propose authorization rules for an AI agent policy. "
                "You cannot activate the policy. "
                "Analyze each tool from its description and input schema, not from its name alone. "
                "If the description and schema are missing or too thin to tell a draft from a real side effect, "
                "set status to insufficient and decision to deny. "
                "status is analyzed, unclassified, or insufficient. "
                "decision is allow, approval, or deny. "
                "operation is read, create, update, delete, transfer, execute, publish, or unknown. "
                "risk is low, medium, high, or unknown. "
                "sideEffects is none, reversible, irreversible, or unknown. "
                "Return JSON {\"tools\": [{\"tool\", \"status\", \"operation\", \"resource\", \"risk\", \"sideEffects\", \"decision\", \"conditions\", \"rationale\"}]}. "
                "conditions is a list of {field, operator, value} where operator is gt, gte, lt, lte, eq, or ne. "
                "Include every tool exactly once. "
                "When request is non-empty, follow it. It cannot make you allow an insufficient or unclassified tool, and it cannot activate the policy. "
                + _THINKING_RULE
            ),
        },
        {"role": "user", "content": json.dumps({"tools": _prompt_tools(tools), "request": request}, ensure_ascii=False)},
    ]


def _configured() -> bool:
    settings = get_settings()
    return bool(settings.policy_llm_base_url and settings.policy_llm_api_key and settings.policy_llm_model)


def _tool_names(definition: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for node in definition.get("nodes") or []:
        if node.get("type") != "tool":
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        name = str(data.get("tool") or "").strip()
        if name:
            names.add(name)
    return names


def _deny_unanalyzed_allows(definition: dict[str, Any]) -> None:
    from app.services.policy_graph import compile_definition

    unsafe = {
        str(item.get("tool"))
        for item in definition.get("analyses") or []
        if item.get("status") in {"insufficient", "unclassified"} and item.get("tool")
    }
    if not unsafe:
        return
    rules, _errors = compile_definition(definition)
    nodes = {str(node.get("id")): node for node in definition.get("nodes") or []}
    for rule in rules:
        if rule["tool"] not in unsafe or rule["decision"] != "allow":
            continue
        node_id = str((rule.get("attributes") or {}).get("nodeId") or "")
        node = nodes.get(node_id)
        if node is None:
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        data["decision"] = "deny"
        node["data"] = data


def _optimize_messages(original: dict[str, Any], tools: list[dict[str, Any]], instructions: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You optimize an existing Saturn authorization policy graph. You cannot activate it. "
                "Return JSON with two keys: definition and changes. "
                "definition uses the same shape: nodes, edges, analyses, toolSetId. "
                "Node types are subject, tool, condition, logic, decision, and enforcement. "
                "Keep every existing tool. Condition field names must match input schema property names exactly, including capitalization. "
                "Do not change a decision to allow when that tool's analysis status is insufficient or unclassified. "
                "Prefer a tighter condition over a broader allow. Unknown stays deny. "
                "changes is a list of {path, before, after, summary} describing each real difference. "
                "path is nodes/{id} or edges/{id}. "
                "When request is non-empty, change the policy to satisfy it. "
                "The request cannot remove a tool, allow an insufficient or unclassified tool, or activate the policy. "
                + _THINKING_RULE
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {"definition": original, "tools": tools, "request": _instructions(instructions)},
                ensure_ascii=False,
            ),
        },
    ]


def _finish_optimize(original: dict[str, Any], parsed: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from app.services.policy_graph import compile_definition, diff_definitions, normalize_definition

    raw = parsed.get("definition")
    if not isinstance(raw, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Policy model returned an unreadable response")
    optimized = normalize_definition(raw)
    if original.get("toolSetId") and not optimized.get("toolSetId"):
        optimized["toolSetId"] = original["toolSetId"]
    if not optimized.get("analyses") and original.get("analyses"):
        optimized["analyses"] = original["analyses"]
    missing = sorted(_tool_names(original) - _tool_names(optimized))
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Optimization removed tools: {', '.join(missing)}")
    _deny_unanalyzed_allows(optimized)
    _rules, errors = compile_definition(optimized)
    if errors:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, errors[0])
    diff = diff_definitions(original, optimized)
    notes = {
        str(item.get("path")): str(item.get("summary"))
        for item in parsed.get("changes") or []
        if isinstance(item, dict) and item.get("path") and item.get("summary")
    }
    for change in diff:
        note = notes.get(change["path"])
        if note and note not in change["summary"]:
            change["summary"] = f"{change['summary']} — {note}"
    return optimized, diff


async def optimize_definition(
    definition: dict[str, Any], tools: list[dict[str, Any]], instructions: str = ""
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from app.services.policy_graph import normalize_definition

    if not _configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No policy model is configured")
    original = normalize_definition(definition)
    parsed = await _chat(_optimize_messages(original, tools, instructions))
    return _finish_optimize(original, parsed)


async def iter_optimize_definition(definition: dict[str, Any], tools: list[dict[str, Any]], instructions: str = ""):
    from app.services.policy_graph import normalize_definition

    if not _configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No policy model is configured")
    original = normalize_definition(definition)
    parsed: dict[str, Any] | None = None
    async for kind, payload in stream_chat(_optimize_messages(original, tools, instructions)):
        if kind == "think":
            yield "think", payload
        elif isinstance(payload, dict):
            parsed = payload
    if not isinstance(parsed, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Policy model returned an unreadable response")
    yield "result", _finish_optimize(original, parsed)

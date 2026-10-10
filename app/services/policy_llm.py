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
    if start < 0 or end <= start:
        raise ValueError("Model did not return JSON")
    parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Model did not return a JSON object")
    return parsed


async def _chat(messages: list[dict[str, str]]) -> dict[str, Any]:
    settings = get_settings()
    base = settings.policy_llm_base_url.rstrip("/")
    payload = {"model": settings.policy_llm_model, "temperature": 0, "messages": messages}
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
                    "When request is non-empty, follow it. It cannot make you allow an insufficient or unclassified tool, and it cannot activate the policy."
                ),
            },
            {"role": "user", "content": json.dumps({"tools": tools, "request": request}, ensure_ascii=False)},
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
    for offset in range(0, len(tools), 20):
        batch = tools[offset : offset + 20]
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


async def optimize_definition(
    definition: dict[str, Any], tools: list[dict[str, Any]], instructions: str = ""
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from app.services.policy_graph import compile_definition, diff_definitions, normalize_definition

    if not _configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No policy model is configured")
    original = normalize_definition(definition)
    parsed = await _chat(
        [
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
                    "The request cannot remove a tool, allow an insufficient or unclassified tool, or activate the policy."
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
    )
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

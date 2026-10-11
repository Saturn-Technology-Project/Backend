"""Canonical policy graph.

The canvas edits this document. The compiler turns each Decision node into a
rule the policy engine can evaluate. A graph that cannot be compiled is still
stored, so a draft can be saved before it is valid.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

_NODE_TYPES = {"subject", "tool", "condition", "logic", "decision", "enforcement"}
_LOGIC = {"and", "or", "not"}
_DECISIONS = {"allow": "allow", "approval": "approval_required", "approval_required": "approval_required", "deny": "deny"}
_OPS = {"gt", "gte", "lt", "lte", "eq", "ne"}


def empty_definition(tool_set_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"nodes": [], "edges": [], "analyses": []}
    if tool_set_id:
        body["toolSetId"] = tool_set_id
    return body


def normalize_definition(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return empty_definition()
    nodes = [item for item in raw.get("nodes") or [] if isinstance(item, dict) and item.get("id") and item.get("type") in _NODE_TYPES]
    edges = [
        item
        for item in raw.get("edges") or []
        if isinstance(item, dict) and item.get("source") and item.get("target")
    ]
    analyses = [item for item in raw.get("analyses") or [] if isinstance(item, dict)]
    body: dict[str, Any] = {"nodes": nodes, "edges": edges, "analyses": analyses}
    set_id = raw.get("toolSetId") or raw.get("tool_set_id")
    if isinstance(set_id, str) and set_id.strip():
        body["toolSetId"] = set_id.strip()
    return body


def _ancestors(node_id: str, incoming: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(incoming.get(node_id, []))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(incoming.get(current, []))
    return seen


def _node_expr(
    node_id: str,
    nodes: dict[str, dict[str, Any]],
    incoming: dict[str, list[str]],
    stack: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if node_id in stack:
        return {"op": "true"}
    node = nodes[node_id]
    kind = node.get("type")
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    stack = stack | {node_id}
    upstream = [
        _node_expr(source, nodes, incoming, stack)
        for source in incoming.get(node_id, [])
        if (nodes.get(source) or {}).get("type") in {"condition", "logic"}
    ]
    if kind == "condition":
        operator = str(data.get("operator") or "eq").lower()
        own = {
            "op": "cmp",
            "field": str(data.get("field") or "").strip(),
            "operator": operator if operator in _OPS else "eq",
            "value": data.get("value"),
        }
        if not upstream:
            return own
        return {"op": "and", "args": [*upstream, own]}
    if kind == "logic":
        op = str(data.get("op") or "and").lower()
        if op not in _LOGIC:
            op = "and"
        return {"op": op, "args": upstream}
    return {"op": "true"}


def _decision_expr(decision_id: str, nodes: dict[str, dict[str, Any]], incoming: dict[str, list[str]]) -> dict[str, Any]:
    exprs = []
    for source in incoming.get(decision_id, []):
        source_node = nodes.get(source)
        if source_node and source_node.get("type") in {"condition", "logic"}:
            exprs.append(_node_expr(source, nodes, incoming))
    if not exprs:
        return {"op": "true"}
    if len(exprs) == 1:
        return exprs[0]
    return {"op": "and", "args": exprs}


def compile_definition(definition: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Compile decision paths into rules. Structural problems are returned, not raised."""
    document = normalize_definition(definition)
    nodes = {str(item["id"]): item for item in document["nodes"]}
    incoming: dict[str, list[str]] = defaultdict(list)
    errors: list[str] = []
    for edge in document["edges"]:
        source = str(edge["source"])
        target = str(edge["target"])
        if source not in nodes or target not in nodes:
            errors.append("An arrow points at a missing node")
            continue
        if source == target:
            errors.append("A node cannot connect to itself")
            continue
        incoming[target].append(source)

    rules: list[dict[str, Any]] = []
    for decision in (item for item in nodes.values() if item.get("type") == "decision"):
        ancestors = _ancestors(str(decision["id"]), incoming)
        tools = [nodes[item] for item in ancestors if nodes[item].get("type") == "tool"]
        if len(tools) != 1:
            errors.append("Each decision must connect back to exactly one tool")
            continue
        data = tools[0].get("data") if isinstance(tools[0].get("data"), dict) else {}
        tool_name = str(data.get("tool") or "").strip()
        if not tool_name:
            errors.append("A tool node is missing its tool name")
            continue
        subjects: list[str] = []
        for item_id in ancestors:
            node = nodes[item_id]
            if node.get("type") != "subject":
                continue
            subject_data = node.get("data") if isinstance(node.get("data"), dict) else {}
            for agent_id in subject_data.get("agentIds") or []:
                if isinstance(agent_id, str) and agent_id and agent_id not in subjects:
                    subjects.append(agent_id)
        decision_data = decision.get("data") if isinstance(decision.get("data"), dict) else {}
        raw_decision = str(decision_data.get("decision") or "deny").strip().lower()
        if raw_decision not in _DECISIONS:
            errors.append(f"Unknown decision on {tool_name}")
            raw_decision = "deny"
        classification = data.get("classification") if isinstance(data.get("classification"), dict) else {}
        enforcement: dict[str, Any] = {}
        for node in nodes.values():
            if node.get("type") != "enforcement":
                continue
            if str(decision["id"]) in _ancestors(str(node["id"]), incoming):
                enforcement = node.get("data") if isinstance(node.get("data"), dict) else {}
                break
        rules.append(
            {
                "id": f"rule_{decision['id']}"[:64],
                "tool": tool_name,
                "expression": _decision_expr(str(decision["id"]), nodes, incoming),
                "decision": _DECISIONS[raw_decision],
                "attributes": {
                    "subjectAgentIds": subjects,
                    "classification": classification,
                    "enforcement": {key: enforcement[key] for key in ("rateLimit",) if key in enforcement},
                    "nodeId": decision["id"],
                    **(
                        {"approvalPolicyId": str(decision_data.get("approvalPolicyId") or decision_data.get("approval_policy_id")).strip()}
                        if str(decision_data.get("approvalPolicyId") or decision_data.get("approval_policy_id") or "").strip()
                        else {}
                    ),
                },
            }
        )
    return rules, errors


def _sample(operator: str, value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if operator == "gt":
        return value + 1
    if operator == "lt":
        return value - 1
    return value


def _satisfy(expr: dict[str, Any]) -> dict[str, Any] | None:
    op = expr.get("op")
    if op == "true":
        return {}
    if op == "cmp":
        field = str(expr.get("field") or "")
        if not field:
            return None
        return {field: _sample(str(expr.get("operator") or "eq"), expr.get("value"))}
    if op == "and":
        merged: dict[str, Any] = {}
        for item in expr.get("args") or []:
            part = _satisfy(item) if isinstance(item, dict) else None
            if part is None:
                return None
            merged.update(part)
        return merged
    if op == "or":
        for item in expr.get("args") or []:
            if isinstance(item, dict):
                part = _satisfy(item)
                if part is not None:
                    return part
        return None
    if op == "not":
        return {}
    return None


def validate_definition(definition: dict[str, Any], set_tools: list[str]) -> dict[str, Any]:
    from app.features.policy.policies.services.policy_engine import evaluate_graph_rules

    document = normalize_definition(definition)
    rules, errors = compile_definition(document)
    issues = [{"level": "error", "code": "structure", "message": item} for item in errors]
    analyses = {str(item.get("tool")): item for item in document["analyses"] if item.get("tool")}
    unclassified: list[str] = []
    insufficient: list[str] = []
    covered = {rule["tool"] for rule in rules}
    expected_tools = set_tools or sorted(covered)
    for tool in expected_tools:
        if tool not in covered:
            issues.append({"level": "error", "code": "coverage", "tool": tool, "message": f"{tool} has no decision"})
        analysis = analyses.get(tool)
        if not analysis:
            continue
        status = analysis.get("status")
        if status == "unclassified":
            unclassified.append(tool)
        elif status == "insufficient":
            insufficient.append(tool)
            issues.append(
                {
                    "level": "warning",
                    "code": "insufficient",
                    "tool": tool,
                    "message": str(analysis.get("rationale") or f"{tool} was not analyzed"),
                }
            )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rule in rules:
        grouped[rule["tool"]].append(rule)
    for tool, group in grouped.items():
        open_rules = [rule for rule in group if (rule.get("expression") or {}).get("op") == "true"]
        decisions = {rule["decision"] for rule in open_rules}
        if len(decisions) > 1:
            issues.append(
                {
                    "level": "error",
                    "code": "conflict",
                    "tool": tool,
                    "message": f"{tool} has unconditional decisions that disagree",
                }
            )
    probes = []
    for rule in rules:
        arguments = _satisfy(rule["expression"])
        if arguments is None:
            probes.append({"tool": rule["tool"], "ok": False, "message": "Could not build a test input for this rule"})
            continue
        result = evaluate_graph_rules([_probe_rule(rule)], arguments, None)
        expected = rule["decision"]
        actual = result.decision.value
        ok = actual == expected
        probes.append(
            {
                "tool": rule["tool"],
                "arguments": arguments,
                "expected": expected,
                "actual": actual,
                "ok": ok,
                "message": "Matched" if ok else "Simulation did not return the decision on this node",
            }
        )
        if not ok:
            issues.append({"level": "error", "code": "simulation", "tool": rule["tool"], "message": probes[-1]["message"]})
    analyzed = [
        tool
        for tool in expected_tools
        if analyses.get(tool, {}).get("status") == "analyzed"
    ]
    return {
        "ok": not any(item["level"] == "error" for item in issues),
        "toolCount": len(expected_tools),
        "analyzed": len(analyzed),
        "unclassified": unclassified,
        "insufficient": insufficient,
        "issues": issues,
        "probes": probes,
    }


def _probe_rule(rule: dict[str, Any]) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        id=rule["id"],
        policy_id=None,
        tool=rule["tool"],
        condition="true",
        conditions=None,
        expression=rule["expression"],
        decision=rule["decision"],
        attributes=rule.get("attributes") or {},
        position=0,
    )


def wire_conditions(
    *,
    tool_id: str,
    decision_id: str,
    conditions: list[Any],
    y: float,
    prefix: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
    """Connect a tool to its decision. Two or more conditions meet at an AND node."""
    valid = [item for item in conditions if isinstance(item, dict) and str(item.get("field") or "").strip()]
    extra_nodes: list[dict[str, Any]] = []
    extra_edges: list[dict[str, Any]] = []
    if not valid:
        extra_edges.append({"id": f"edge_{tool_id}_{decision_id}", "source": tool_id, "target": decision_id})
        return extra_nodes, extra_edges, 280
    span = (len(valid) - 1) * 72
    cond_ids: list[str] = []
    for index, condition in enumerate(valid):
        cond_id = f"condition_{prefix}_{index}"
        cond_ids.append(cond_id)
        extra_nodes.append(
            {
                "id": cond_id,
                "type": "condition",
                "position": {"x": 280, "y": y - span / 2 + index * 72},
                "data": {
                    "field": str(condition.get("field")),
                    "operator": str(condition.get("operator") or "eq"),
                    "value": condition.get("value"),
                },
            }
        )
        extra_edges.append({"id": f"edge_{tool_id}_{cond_id}", "source": tool_id, "target": cond_id})
    if len(cond_ids) == 1:
        extra_edges.append({"id": f"edge_{cond_ids[0]}_{decision_id}", "source": cond_ids[0], "target": decision_id})
        return extra_nodes, extra_edges, 500
    logic_id = f"logic_{prefix}"
    extra_nodes.append(
        {
            "id": logic_id,
            "type": "logic",
            "position": {"x": 500, "y": y},
            "data": {"op": "and"},
        }
    )
    for cond_id in cond_ids:
        extra_edges.append({"id": f"edge_{cond_id}_{logic_id}", "source": cond_id, "target": logic_id})
    extra_edges.append({"id": f"edge_{logic_id}_{decision_id}", "source": logic_id, "target": decision_id})
    return extra_nodes, extra_edges, 720


def definition_from_analyses(tool_set_id: str, analyses: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for index, analysis in enumerate(analyses):
        tool = str(analysis.get("tool") or "").strip()
        if not tool:
            continue
        y = index * 132
        tool_id = f"tool_{index}"
        decision_id = f"decision_{index}"
        nodes.append(
            {
                "id": tool_id,
                "type": "tool",
                "position": {"x": 40, "y": y},
                "data": {
                    "tool": tool,
                    "label": tool,
                    "classification": {
                        "status": analysis.get("status") or "insufficient",
                        "operation": analysis.get("operation") or "unknown",
                        "resource": analysis.get("resource") or "",
                        "risk": analysis.get("risk") or "unknown",
                        "sideEffects": analysis.get("sideEffects") or "unknown",
                        "rationale": analysis.get("rationale") or "",
                    },
                },
            }
        )
        decision = str(analysis.get("decision") or "deny")
        gate_nodes, gate_edges, decision_x = wire_conditions(
            tool_id=tool_id,
            decision_id=decision_id,
            conditions=analysis.get("conditions") or [],
            y=y,
            prefix=str(index),
        )
        nodes.extend(gate_nodes)
        nodes.append(
            {
                "id": decision_id,
                "type": "decision",
                "position": {"x": decision_x, "y": y},
                "data": {"decision": decision if decision in {"allow", "approval", "deny"} else "deny"},
            }
        )
        edges.extend(gate_edges)
    return {"toolSetId": tool_set_id, "nodes": nodes, "edges": edges, "analyses": analyses}


def risky_allows(rules: list[Any]) -> list[str]:
    names: list[str] = []
    for rule in rules:
        decision = str(getattr(rule, "decision", "") or "")
        if decision != "allow":
            continue
        attributes = getattr(rule, "attributes", None)
        classification = attributes.get("classification") if isinstance(attributes, dict) else {}
        if not isinstance(classification, dict):
            classification = {}
        risk = str(classification.get("risk") or "")
        if risk in {"high", "unknown"} and getattr(rule, "tool", None) not in names:
            names.append(str(rule.tool))
    return names


def _node_brief(node: dict[str, Any]) -> dict[str, Any]:
    return {"type": node.get("type"), "data": node.get("data") or {}}


def _node_label(node: dict[str, Any]) -> str:
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    return str(data.get("tool") or data.get("decision") or data.get("field") or data.get("op") or "")


def _change_summary(old: dict[str, Any], new: dict[str, Any]) -> str:
    old_data = old.get("data") if isinstance(old.get("data"), dict) else {}
    new_data = new.get("data") if isinstance(new.get("data"), dict) else {}
    bits: list[str] = []
    if old.get("type") != new.get("type"):
        bits.append(f"type: {old.get('type')} → {new.get('type')}")
    for key in sorted(set(old_data) | set(new_data)):
        if old_data.get(key) != new_data.get(key):
            bits.append(f"{key}: {old_data.get(key)} → {new_data.get(key)}")
    label = _node_label(new) or _node_label(old)
    detail = "; ".join(bits) or "updated"
    return f"{label} {detail}".strip()


def diff_definitions(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Authorization differences. Node positions are layout, so they are ignored."""
    original = normalize_definition(before)
    updated = normalize_definition(after)
    changes: list[dict[str, Any]] = []
    before_nodes = {str(node["id"]): node for node in original["nodes"]}
    after_nodes = {str(node["id"]): node for node in updated["nodes"]}
    for ident, node in after_nodes.items():
        if ident not in before_nodes:
            label = _node_label(node)
            changes.append(
                {
                    "kind": "added",
                    "path": f"nodes/{ident}",
                    "before": None,
                    "after": _node_brief(node),
                    "summary": f"Added {node.get('type')} {label}".strip(),
                }
            )
            continue
        old = before_nodes[ident]
        if old.get("type") != node.get("type") or (old.get("data") or {}) != (node.get("data") or {}):
            changes.append(
                {
                    "kind": "changed",
                    "path": f"nodes/{ident}",
                    "before": _node_brief(old),
                    "after": _node_brief(node),
                    "summary": _change_summary(old, node),
                }
            )
    for ident, node in before_nodes.items():
        if ident not in after_nodes:
            label = _node_label(node)
            changes.append(
                {
                    "kind": "removed",
                    "path": f"nodes/{ident}",
                    "before": _node_brief(node),
                    "after": None,
                    "summary": f"Removed {node.get('type')} {label}".strip(),
                }
            )
    before_edges = {str(edge.get("id")): edge for edge in original["edges"]}
    after_edges = {str(edge.get("id")): edge for edge in updated["edges"]}
    for ident, edge in after_edges.items():
        if ident not in before_edges:
            changes.append(
                {
                    "kind": "added",
                    "path": f"edges/{ident}",
                    "before": None,
                    "after": {"source": edge.get("source"), "target": edge.get("target")},
                    "summary": f"Connected {edge.get('source')} to {edge.get('target')}",
                }
            )
    for ident, edge in before_edges.items():
        if ident not in after_edges:
            changes.append(
                {
                    "kind": "removed",
                    "path": f"edges/{ident}",
                    "before": {"source": edge.get("source"), "target": edge.get("target")},
                    "after": None,
                    "summary": f"Removed connection {edge.get('source')} to {edge.get('target')}",
                }
            )
    return changes

"""Canonical policy graph.

The canvas edits this document. The compiler turns each Decision node into a
rule the policy engine can evaluate. A graph that cannot be compiled is still
stored, so a draft can be saved before it is valid.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any

from app.features.policy.policies.services.limits import canonical_operator

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


class PolicyGraphTooLarge(ValueError):
    """The graph is over a size or complexity limit. The service answers 400."""


MAX_NODES = 2_000
MAX_EDGES = 4_000
MAX_IN_DEGREE = 128
MAX_NODE_ID = 128
# One rule's expression, counted after shared conditions are expanded.
MAX_EXPRESSION_SIZE = 2_000
MAX_EXPRESSION_DEPTH = 64
MAX_TOTAL_EXPRESSION_SIZE = 20_000
# Agent ids copied from subject nodes into all rules together.
MAX_TOTAL_SUBJECT_IDS = 20_000

_GATES = {"condition", "logic"}
_MANY = "__many__"


def _data(node: dict[str, Any]) -> dict[str, Any]:
    data = node.get("data")
    return data if isinstance(data, dict) else {}


def _topological_order(
    nodes: dict[str, dict[str, Any]], incoming: dict[str, list[str]], outgoing: dict[str, list[str]]
) -> list[str] | None:
    """Kahn's algorithm. None means the graph has a cycle."""
    pending = {ident: len(incoming.get(ident, [])) for ident in nodes}
    ready = [ident for ident, count in pending.items() if count == 0]
    order: list[str] = []
    while ready:
        current = ready.pop()
        order.append(current)
        for target in outgoing.get(current, []):
            pending[target] -= 1
            if pending[target] == 0:
                ready.append(target)
    return order if len(order) == len(nodes) else None


def _gate_expressions(
    order: list[str],
    nodes: dict[str, dict[str, Any]],
    incoming: dict[str, list[str]],
    errors: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Build every condition and logic expression once, in topological order.

    Shared upstream gates are built once and reused, and the expanded size and
    depth are tracked so a fan-in graph cannot grow an exponential rule.
    """
    exprs: dict[str, dict[str, Any]] = {}
    sizes: dict[str, int] = {}
    depths: dict[str, int] = {}
    for node_id in order:
        node = nodes[node_id]
        kind = node.get("type")
        if kind not in _GATES:
            continue
        data = _data(node)
        upstream = [source for source in incoming.get(node_id, []) if nodes[source].get("type") in _GATES]
        upstream_size = sum(sizes[source] for source in upstream)
        upstream_depth = max((depths[source] for source in upstream), default=0)
        if kind == "condition":
            raw_operator = data.get("operator") if data.get("operator") is not None else "eq"
            operator = canonical_operator(raw_operator)
            if operator is None:
                errors.append(f"Unknown operator on condition {str(data.get('field') or node_id)[:64]}")
                operator = "eq"
            own = {
                "op": "cmp",
                "field": str(data.get("field") or "").strip(),
                "operator": operator,
                "value": data.get("value"),
            }
            if upstream:
                expr = {"op": "and", "args": [*(exprs[source] for source in upstream), own]}
                size = upstream_size + 2
                depth = max(upstream_depth, 1) + 1
            else:
                expr, size, depth = own, 1, 1
        else:
            raw_op = data.get("op")
            op = str(raw_op or "and").lower()
            if op not in _LOGIC:
                errors.append(f"Unknown logic operator {str(raw_op)[:32]}")
                op = "and"
            expr = {"op": op, "args": [exprs[source] for source in upstream]}
            size = upstream_size + 1
            depth = upstream_depth + 1
        if size > MAX_EXPRESSION_SIZE or depth > MAX_EXPRESSION_DEPTH:
            raise PolicyGraphTooLarge(
                "The policy graph is too complex: a decision path may combine at most "
                f"{MAX_EXPRESSION_SIZE} checks, nested at most {MAX_EXPRESSION_DEPTH} deep"
            )
        exprs[node_id] = expr
        sizes[node_id] = size
        depths[node_id] = depth
    return exprs, sizes


def _merge_tools(current: Any, more: Any) -> Any:
    """Track the tool ancestors of a node, stopping at two: only 'exactly one' matters."""
    if current == _MANY or more == _MANY:
        return _MANY
    merged = current | more
    return _MANY if len(merged) > 1 else merged


def check_definition_limits(document: dict[str, Any]) -> None:
    if len(document.get("nodes") or []) > MAX_NODES:
        raise PolicyGraphTooLarge(f"The policy graph is too large: at most {MAX_NODES} nodes")
    if len(document.get("edges") or []) > MAX_EDGES:
        raise PolicyGraphTooLarge(f"The policy graph is too large: at most {MAX_EDGES} arrows")
    for node in document.get("nodes") or []:
        if len(str(node.get("id"))) > MAX_NODE_ID:
            raise PolicyGraphTooLarge(f"Node ids must be at most {MAX_NODE_ID} characters")


def compile_definition(definition: dict[str, Any], *, strict: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """Compile decision paths into rules. Structural problems are returned, not raised.

    A graph over the size limits compiles to no rules. With strict=True that
    raises PolicyGraphTooLarge instead, so a save can be refused with 400.
    """
    document = normalize_definition(definition)
    try:
        return _compile(document)
    except PolicyGraphTooLarge as exc:
        if strict:
            raise
        return [], [str(exc)]


def _compile(document: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    check_definition_limits(document)
    nodes = {str(item["id"]): item for item in document["nodes"]}
    incoming: dict[str, list[str]] = defaultdict(list)
    outgoing: dict[str, list[str]] = defaultdict(list)
    errors: list[str] = []
    seen_edges: set[tuple[str, str]] = set()
    for edge in document["edges"]:
        source = str(edge["source"])
        target = str(edge["target"])
        if source not in nodes or target not in nodes:
            errors.append("An arrow points at a missing node")
            continue
        if source == target:
            errors.append("A node cannot connect to itself")
            continue
        if (source, target) in seen_edges:
            continue
        seen_edges.add((source, target))
        incoming[target].append(source)
        outgoing[source].append(target)
        if len(incoming[target]) > MAX_IN_DEGREE:
            raise PolicyGraphTooLarge(
                f"The policy graph is too large: a node may have at most {MAX_IN_DEGREE} incoming arrows"
            )

    order = _topological_order(nodes, incoming, outgoing)
    if order is None:
        errors.append("The policy graph has a cycle")
        return [], errors

    exprs, sizes = _gate_expressions(order, nodes, incoming, errors)

    # Tool and subject ancestors of every node, propagated once in topological order.
    empty: frozenset[str] = frozenset()
    tool_ancestors: dict[str, Any] = {}
    subject_ancestors: dict[str, frozenset[str]] = {}
    for node_id in order:
        tools: Any = empty
        subjects: frozenset[str] = empty
        for source in incoming.get(node_id, []):
            source_tools = tool_ancestors[source]
            if nodes[source].get("type") == "tool":
                source_tools = _merge_tools(source_tools, frozenset({source}))
            tools = _merge_tools(tools, source_tools)
            source_subjects = subject_ancestors[source]
            if nodes[source].get("type") == "subject":
                source_subjects = source_subjects | {source}
            if source_subjects and source_subjects is not subjects:
                subjects = source_subjects if not subjects else subjects | source_subjects
        tool_ancestors[node_id] = tools
        subject_ancestors[node_id] = subjects

    # The first enforcement node, in document order, downstream of each decision.
    # A node already reached from an earlier enforcement node had all of its
    # ancestors reached then too, so each node is visited once in total.
    enforcement_for: dict[str, dict[str, Any]] = {}
    visited: set[str] = set()
    for node in nodes.values():
        if node.get("type") != "enforcement":
            continue
        data = _data(node)
        stack = list(incoming.get(str(node["id"]), []))
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            if nodes[current].get("type") == "decision":
                enforcement_for.setdefault(current, data)
            stack.extend(incoming.get(current, []))

    rules: list[dict[str, Any]] = []
    total_size = 0
    total_subjects = 0
    for decision in (item for item in nodes.values() if item.get("type") == "decision"):
        decision_id = str(decision["id"])
        tools = tool_ancestors.get(decision_id, empty)
        if tools == _MANY or len(tools) != 1:
            errors.append("Each decision must connect back to exactly one tool")
            continue
        data = _data(nodes[next(iter(tools))])
        tool_name = str(data.get("tool") or "").strip()
        if not tool_name:
            errors.append("A tool node is missing its tool name")
            continue
        agent_ids: dict[str, None] = {}
        for subject_id in sorted(subject_ancestors.get(decision_id, empty)):
            for agent_id in _data(nodes[subject_id]).get("agentIds") or []:
                if isinstance(agent_id, str) and agent_id:
                    agent_ids.setdefault(agent_id, None)
        total_subjects += len(agent_ids)
        if total_subjects > MAX_TOTAL_SUBJECT_IDS:
            raise PolicyGraphTooLarge(
                f"The policy graph is too complex: all decisions together may name at most {MAX_TOTAL_SUBJECT_IDS} agents"
            )
        decision_data = _data(decision)
        raw_decision = str(decision_data.get("decision") or "deny").strip().lower()
        if raw_decision not in _DECISIONS:
            errors.append(f"Unknown decision on {tool_name}")
            raw_decision = "deny"
        classification = data.get("classification") if isinstance(data.get("classification"), dict) else {}
        enforcement = enforcement_for.get(decision_id, {})
        gates = [source for source in incoming.get(decision_id, []) if nodes[source].get("type") in _GATES]
        if not gates:
            expression: dict[str, Any] = {"op": "true"}
            size = 1
        elif len(gates) == 1:
            expression = exprs[gates[0]]
            size = sizes[gates[0]]
        else:
            expression = {"op": "and", "args": [exprs[source] for source in gates]}
            size = 1 + sum(sizes[source] for source in gates)
        if size > MAX_EXPRESSION_SIZE:
            raise PolicyGraphTooLarge(
                f"The policy graph is too complex: a decision path may combine at most {MAX_EXPRESSION_SIZE} checks"
            )
        total_size += size
        if total_size > MAX_TOTAL_EXPRESSION_SIZE:
            raise PolicyGraphTooLarge(
                "The policy graph is too complex: all decisions together may combine at most "
                f"{MAX_TOTAL_EXPRESSION_SIZE} checks"
            )
        approval_policy = str(
            decision_data.get("approvalPolicyId") or decision_data.get("approval_policy_id") or ""
        ).strip()
        rules.append(
            {
                "id": f"rule_{decision['id']}"[:64],
                "tool": tool_name,
                # A plain tree per rule, so stored rules never alias each other.
                "expression": copy.deepcopy(expression),
                "decision": _DECISIONS[raw_decision],
                "attributes": {
                    "subjectAgentIds": list(agent_ids),
                    "classification": classification,
                    "enforcement": {key: enforcement[key] for key in ("rateLimit",) if key in enforcement},
                    "nodeId": decision["id"],
                    **({"approvalPolicyId": approval_policy} if approval_policy else {}),
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
    document = normalize_definition(definition)
    rules, errors = compile_definition(document)
    return validate_rules(rules, set_tools, analyses=document["analyses"], structure_errors=errors)


def _is_open(rule: dict[str, Any]) -> bool:
    return (rule.get("expression") or {}).get("op") == "true"


def _has_subjects(rule: dict[str, Any]) -> bool:
    attributes = rule.get("attributes") if isinstance(rule.get("attributes"), dict) else {}
    return bool(attributes.get("subjectAgentIds"))


def _shadow_issues(tool: str, group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """An unconditional allow beside a conditional restriction for the same tool.

    A condition only matches when the call carries that field under exactly
    that name. A call that leaves the field out, or spells it differently,
    skips the restriction and falls through to the allow.
    """
    open_allow = any(rule["decision"] == "allow" and _is_open(rule) and not _has_subjects(rule) for rule in group)
    restricted = sorted({rule["decision"] for rule in group if rule["decision"] != "allow" and not _is_open(rule)})
    if not open_allow or not restricted:
        return []
    labels = " and ".join("approval" if item == "approval_required" else item for item in restricted)
    return [
        {
            "level": "warning",
            "code": "shadowed",
            "tool": tool,
            "message": (
                f"{tool} is allowed without conditions, so its conditional {labels} only applies when a call "
                "includes those fields with the exact same names. A call that leaves them out is allowed."
            ),
        }
    ]


def validate_rules(
    rules: list[dict[str, Any]],
    set_tools: list[str],
    *,
    analyses: list[dict[str, Any]] | None = None,
    structure_errors: list[str] | None = None,
    ordered: bool = False,
) -> dict[str, Any]:
    """Check compiled rules. ordered=True checks legacy rules, where the first match by position wins."""
    from app.features.policy.policies.services.policy_engine import evaluate_graph_rules, evaluate_rules

    issues = [{"level": "error", "code": "structure", "message": item} for item in structure_errors or []]
    by_tool = {str(item.get("tool")): item for item in analyses or [] if isinstance(item, dict) and item.get("tool")}
    unclassified: list[str] = []
    insufficient: list[str] = []
    covered = {rule["tool"] for rule in rules}
    expected_tools = set_tools or sorted(covered)
    for tool in expected_tools:
        if tool not in covered:
            issues.append({"level": "error", "code": "coverage", "tool": tool, "message": f"{tool} has no decision"})
        analysis = by_tool.get(tool)
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
                    "message": str(analysis.get("rationale") or f"{tool} was not analyzed")[:1000],
                }
            )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rule in rules:
        grouped[rule["tool"]].append(rule)
    for tool, group in grouped.items():
        if ordered:
            group.sort(key=lambda item: item.get("position") or 0)
            first_open = next((index for index, rule in enumerate(group) if _is_open(rule)), None)
            if first_open is not None and first_open < len(group) - 1:
                issues.append(
                    {
                        "level": "warning",
                        "code": "unreachable",
                        "tool": tool,
                        "message": f"{tool} has rules after an unconditional rule. They never apply.",
                    }
                )
        else:
            decisions = {rule["decision"] for rule in group if _is_open(rule)}
            if len(decisions) > 1:
                issues.append(
                    {
                        "level": "error",
                        "code": "conflict",
                        "tool": tool,
                        "message": f"{tool} has unconditional decisions that disagree",
                    }
                )
        issues.extend(_shadow_issues(tool, group))
    probes = []
    for rule in rules:
        arguments = _satisfy(rule["expression"])
        if arguments is None:
            probes.append({"tool": rule["tool"], "ok": False, "message": "Could not build a test input for this rule"})
            continue
        if ordered:
            result = evaluate_rules([_probe_rule(item) for item in grouped[rule["tool"]]], arguments)
        else:
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
        if not ok and not ordered:
            issues.append({"level": "error", "code": "simulation", "tool": rule["tool"], "message": probes[-1]["message"]})
    analyzed = [tool for tool in expected_tools if by_tool.get(tool, {}).get("status") == "analyzed"]
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
        position=rule.get("position") or 0,
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
    """Tools that activation must confirm: every tool with an allow rule.

    The risk label on a tool comes from the policy model, or from a definition
    the client sent back after the model edited it. Either way it can be
    steered by a tool description, so it is advisory and cannot waive the
    confirmation. Tools labelled high or unknown risk are listed first.
    """
    flagged: list[str] = []
    others: list[str] = []
    for rule in rules:
        decision = str(getattr(rule, "decision", "") or "")
        if decision != "allow":
            continue
        tool = str(getattr(rule, "tool", "") or "")
        if tool in flagged or tool in others:
            continue
        attributes = getattr(rule, "attributes", None)
        classification = attributes.get("classification") if isinstance(attributes, dict) else {}
        if not isinstance(classification, dict):
            classification = {}
        risk = str(classification.get("risk") or "unknown")
        (others if risk in {"low", "medium"} else flagged).append(tool)
    for tool in others:
        if tool in flagged:
            continue
        flagged.append(tool)
    return flagged


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

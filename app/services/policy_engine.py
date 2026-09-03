from __future__ import annotations

import operator
import re
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Agent, Policy, PolicyRule, Tool
from app.models.enums import PolicyDecision

_CONDITION = re.compile(
    r"^(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<op><=|>=|==|!=|<|>)\s*(?P<value>.+)$"
)
_AND_SPLIT = re.compile(r"\s+AND\s+", re.I)
_OPS = {
    "<=": operator.le,
    ">=": operator.ge,
    "<": operator.lt,
    ">": operator.gt,
    "==": operator.eq,
    "!=": operator.ne,
}
_OPERATOR_SYMBOLS = {
    "lte": "<=",
    "gte": ">=",
    "lt": "<",
    "gt": ">",
    "eq": "==",
    "ne": "!=",
    "le": "<=",
    "ge": ">=",
}
_SYMBOL_OPERATORS = {symbol: name for name, symbol in _OPERATOR_SYMBOLS.items() if name in {"lte", "gte", "lt", "gt", "eq", "ne"}}
_DECISION_ALIASES = {
    "allow": PolicyDecision.ALLOW,
    "approval": PolicyDecision.APPROVAL_REQUIRED,
    "approval_required": PolicyDecision.APPROVAL_REQUIRED,
    "deny": PolicyDecision.DENY,
}


@dataclass(frozen=True)
class EvaluationResult:
    decision: PolicyDecision
    matched_rule: PolicyRule | None = None
    policy_id: str | None = None
    policy_version: int | None = None
    matched_rule_id: str | None = None
    reason: str | None = None


def normalize_decision(raw: str) -> PolicyDecision:
    key = raw.strip().lower()
    if key not in _DECISION_ALIASES:
        raise ValueError(f"Unknown policy decision: {raw}")
    return _DECISION_ALIASES[key]


def parse_literal(raw: str) -> Any:
    value = raw.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def coerce_compare(left: Any, right: Any) -> tuple[Any, Any]:
    if isinstance(left, bool) or isinstance(right, bool):
        return left, right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left), float(right)
    if isinstance(left, (int, float)):
        try:
            return float(left), float(right)
        except (TypeError, ValueError):
            return str(left), str(right)
    if isinstance(right, (int, float)):
        try:
            return float(left), float(right)
        except (TypeError, ValueError):
            return str(left), str(right)
    return left, right


def compile_condition(conditions: list[dict[str, Any]] | None) -> str:
    if not conditions:
        return "true"
    parts: list[str] = []
    for item in conditions:
        field = str(item.get("field") or "").strip()
        if not field:
            continue
        operator = str(item.get("operator") or "eq").strip().lower()
        symbol = _OPERATOR_SYMBOLS.get(operator, operator if operator in _OPS else "==")
        value = item.get("value")
        if isinstance(value, str):
            rendered = value if re.fullmatch(r"-?\d+(\.\d+)?", value.strip()) else repr(value)
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        parts.append(f"{field} {symbol} {rendered}")
    return " AND ".join(parts) if parts else "true"


def parse_condition_string(condition: str) -> list[dict[str, Any]]:
    text = (condition or "").strip()
    if not text or text.lower() in {"true", "*"}:
        return []
    parsed: list[dict[str, Any]] = []
    for atom in _AND_SPLIT.split(text):
        match = _CONDITION.match(atom.strip())
        if not match:
            continue
        parsed.append(
            {
                "field": match.group("field"),
                "operator": _SYMBOL_OPERATORS.get(match.group("op"), match.group("op")),
                "value": parse_literal(match.group("value")),
            }
        )
    return parsed


def conditions_for(rule: PolicyRule | Any) -> list[dict[str, Any]]:
    structured = getattr(rule, "conditions", None)
    if isinstance(structured, list) and structured:
        return [item for item in structured if isinstance(item, dict)]
    return parse_condition_string(getattr(rule, "condition", "") or "")


def _format_money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return f"${int(number):,}"
    return f"${number:,.2f}"


def describe_condition(item: dict[str, Any]) -> str:
    field = str(item.get("field") or "")
    operator = str(item.get("operator") or "eq")
    symbol = _OPERATOR_SYMBOLS.get(operator, operator)
    value = item.get("value")
    rendered = _format_money(value) if field == "amount" else str(value)
    return f"{field} {symbol} {rendered}"


def describe_rule(rule: PolicyRule | Any) -> str:
    conds = conditions_for(rule)
    if not conds:
        return getattr(rule, "condition", "") or "true"
    return " AND ".join(describe_condition(item) for item in conds)


def describe_reason(rule: PolicyRule | Any, arguments: dict[str, Any] | None = None) -> str:
    del arguments
    conds = conditions_for(rule)
    amount_gt = next(
        (
            item
            for item in conds
            if item.get("field") == "amount" and str(item.get("operator")) in {"gt", "gte"}
        ),
        None,
    )
    tool = str(getattr(rule, "tool", "") or "")
    if amount_gt is not None:
        label = "Refund amount" if "refund" in tool.lower() else "Amount"
        return f"{label} exceeds {_format_money(amount_gt.get('value'))}"
    if not conds:
        return "Matched policy rule"
    return " and ".join(describe_condition(item) for item in conds)


def evaluate_structured(item: dict[str, Any], arguments: dict[str, Any]) -> bool:
    field = str(item.get("field") or "")
    if not field or field not in arguments or arguments[field] is None:
        return False
    operator = str(item.get("operator") or "eq").strip().lower()
    symbol = _OPERATOR_SYMBOLS.get(operator, operator if operator in _OPS else "==")
    op = _OPS.get(symbol)
    if op is None:
        return False
    left, right = coerce_compare(arguments[field], item.get("value"))
    return bool(op(left, right))


def _evaluate_atom_string(condition: str, arguments: dict[str, Any]) -> bool:
    text = condition.strip()
    if not text or text.lower() in {"true", "*"}:
        return True
    match = _CONDITION.match(text)
    if not match:
        return False
    field = match.group("field")
    if field not in arguments or arguments[field] is None:
        return False
    op = _OPS[match.group("op")]
    left, right = coerce_compare(arguments[field], parse_literal(match.group("value")))
    return bool(op(left, right))


def evaluate_condition(condition: str, arguments: dict[str, Any]) -> bool:
    text = condition.strip()
    if not text:
        return True
    parts = _AND_SPLIT.split(text)
    return all(_evaluate_atom_string(part, arguments) for part in parts)


def match_rule(rule: PolicyRule | Any, arguments: dict[str, Any]) -> bool:
    structured = getattr(rule, "conditions", None)
    if isinstance(structured, list) and structured:
        return all(evaluate_structured(item, arguments) for item in structured if isinstance(item, dict))
    return evaluate_condition(getattr(rule, "condition", "") or "true", arguments)


def evaluate_rules(
    rules: list[PolicyRule],
    arguments: dict[str, Any],
) -> EvaluationResult:
    if not rules:
        return EvaluationResult(decision=PolicyDecision.ALLOW)

    for rule in sorted(rules, key=lambda item: item.position):
        if match_rule(rule, arguments):
            return EvaluationResult(
                decision=normalize_decision(rule.decision),
                matched_rule=rule,
                policy_id=getattr(rule, "policy_id", None),
                matched_rule_id=getattr(rule, "id", None),
                reason=describe_reason(rule, arguments),
            )
    return EvaluationResult(decision=PolicyDecision.DENY)


def _policy_applies(policy: Policy, agent: Agent) -> bool:
    if (policy.status or "active").lower() != "active":
        return False
    agents = list(getattr(policy, "agents", None) or [])
    if not agents:
        return True
    return any(item.id == agent.id for item in agents)


class PolicyEngine:
    async def evaluate(
        self,
        session: AsyncSession,
        *,
        organization_id: str,
        agent: Agent,
        tool: Tool,
        arguments: dict[str, Any],
    ) -> EvaluationResult:
        policies = await self._policies(session, organization_id)
        rules: list[PolicyRule] = []
        applicable: dict[str, Policy] = {}
        for policy in policies:
            if not _policy_applies(policy, agent):
                continue
            applicable[policy.id] = policy
            for rule in policy.rules:
                if rule.tool == tool.name:
                    rules.append(rule)
        result = evaluate_rules(rules, arguments)
        if result.policy_id and result.policy_id in applicable:
            policy = applicable[result.policy_id]
            result = replace(result, policy_version=policy.version or 1)
        return result

    async def _policies(self, session: AsyncSession, organization_id: str) -> list[Policy]:
        result = await session.execute(
            select(Policy)
            .options(selectinload(Policy.rules), selectinload(Policy.agents))
            .where(Policy.organization_id == organization_id)
            .order_by(Policy.created_at.asc())
        )
        return list(result.scalars().unique().all())


policy_engine = PolicyEngine()

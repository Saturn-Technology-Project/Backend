from __future__ import annotations

import math
import operator
import re
import unicodedata
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Agent, Policy, PolicyRule, Tool
from app.core.enums import PolicyDecision

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
    policy_name: str | None = None
    policy_version: int | None = None
    matched_rule_id: str | None = None
    reason: str | None = None
    approval_policy_id: str | None = None


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


_NUMERIC_TEXT = re.compile(r"-?\d+(\.\d+)?")
_ORDERING = {"<", "<=", ">", ">="}


def strict_number(value: Any) -> Decimal | None:
    """A finite number, or a string written exactly as one ("1000", "-2.5"). Anything else is None.

    "+1000", " 1000", "1e3", "$1000", NaN, Infinity and booleans are not numbers here, so an agent
    cannot dodge a numeric rule by spelling a large amount in a way only the upstream understands.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, str) and _NUMERIC_TEXT.fullmatch(value):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None


def coerce_compare(left: Any, right: Any) -> tuple[Any, Any]:
    """Kept for callers that only need comparable operands. Prefer compare()."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left, right
    numbers = strict_number(left), strict_number(right)
    if numbers[0] is not None and numbers[1] is not None:
        return numbers
    return left, right


def _text(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def compare(symbol: str, left: Any, right: Any) -> bool | None:
    """True or False when the operands compare cleanly, None when they cannot be compared.

    None means the engine cannot tell. match_rule then treats a deny or approval rule as matched
    and an allow rule as not matched, so a value the engine does not understand never loosens a
    restriction.
    """
    op = _OPS.get(symbol)
    if op is None:
        return None
    if left is None or isinstance(left, (dict, list, tuple, set)):
        return None
    numbers = strict_number(left), strict_number(right)
    if symbol in _ORDERING:
        if numbers[0] is None or numbers[1] is None:
            return None
        return bool(op(numbers[0], numbers[1]))
    if numbers[0] is not None and numbers[1] is not None:
        return bool(op(numbers[0], numbers[1]))
    if isinstance(left, bool) or isinstance(right, bool):
        if isinstance(left, bool) and isinstance(right, bool):
            return bool(op(left, right))
        return None
    if isinstance(left, str) and isinstance(right, str):
        return bool(op(_text(left), _text(right)))
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        # One side is a number the other side cannot be read as: not comparable.
        return None
    return bool(op(left, right))


def _and(values) -> bool | None:
    result: bool | None = True
    for value in values:
        if value is False:
            return False
        if value is None:
            result = None
    return result


def _or(values) -> bool | None:
    result: bool | None = False
    for value in values:
        if value is True:
            return True
        if value is None:
            result = None
    return result


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


def structured_truth(item: dict[str, Any], arguments: dict[str, Any]) -> bool | None:
    field = str(item.get("field") or "")
    if not field:
        return False
    operator = str(item.get("operator") or "eq").strip().lower()
    symbol = _OPERATOR_SYMBOLS.get(operator, operator if operator in _OPS else "==")
    if symbol not in _OPS:
        return None
    if field not in arguments:
        # The call does not say. A restriction on this field cannot be ruled out.
        return None
    return compare(symbol, arguments[field], item.get("value"))


def evaluate_structured(item: dict[str, Any], arguments: dict[str, Any]) -> bool:
    """Two-valued view for display and simulation: only a clean True counts."""
    return structured_truth(item, arguments) is True


def _atom_truth(condition: str, arguments: dict[str, Any]) -> bool | None:
    text = condition.strip()
    if not text or text.lower() in {"true", "*"}:
        return True
    match = _CONDITION.match(text)
    if not match:
        return None
    field = match.group("field")
    if field not in arguments:
        return None
    return compare(match.group("op"), arguments[field], parse_literal(match.group("value")))


def condition_truth(condition: str, arguments: dict[str, Any]) -> bool | None:
    text = condition.strip()
    if not text:
        return True
    return _and(_atom_truth(part, arguments) for part in _AND_SPLIT.split(text))


def evaluate_condition(condition: str, arguments: dict[str, Any]) -> bool:
    return condition_truth(condition, arguments) is True


def expr_truth(expr: dict[str, Any] | None, arguments: dict[str, Any]) -> bool | None:
    if not isinstance(expr, dict) or not expr:
        return False
    op = expr.get("op")
    if op == "true":
        return True
    if op == "cmp":
        return structured_truth(
            {"field": expr.get("field"), "operator": expr.get("operator"), "value": expr.get("value")},
            arguments,
        )
    args = [item for item in expr.get("args") or [] if isinstance(item, dict)]
    if op == "not":
        if len(args) != 1:
            return False
        inner = expr_truth(args[0], arguments)
        return None if inner is None else not inner
    if op == "and":
        return _and(expr_truth(item, arguments) for item in args) if args else True
    if op == "or":
        return _or(expr_truth(item, arguments) for item in args)
    return False


def eval_expr(expr: dict[str, Any] | None, arguments: dict[str, Any]) -> bool:
    return expr_truth(expr, arguments) is True


def _restrictive(rule: PolicyRule | Any) -> bool:
    try:
        return normalize_decision(str(getattr(rule, "decision", "") or "")) != PolicyDecision.ALLOW
    except ValueError:
        return True


def rule_truth(rule: PolicyRule | Any, arguments: dict[str, Any]) -> bool | None:
    expression = getattr(rule, "expression", None)
    if isinstance(expression, dict) and expression:
        return expr_truth(expression, arguments)
    structured = getattr(rule, "conditions", None)
    if isinstance(structured, list) and structured:
        return _and(structured_truth(item, arguments) for item in structured if isinstance(item, dict))
    return condition_truth(getattr(rule, "condition", "") or "true", arguments)


def _subject_excluded(rule: PolicyRule | Any, agent_id: str | None) -> bool:
    attributes = getattr(rule, "attributes", None)
    if isinstance(attributes, dict) and agent_id is not None:
        subjects = attributes.get("subjectAgentIds") or []
        if subjects and agent_id not in subjects:
            return True
    return False


def match_truth(rule: PolicyRule | Any, arguments: dict[str, Any], agent_id: str | None = None) -> bool | None:
    """True, False, or None when the call's values cannot be compared with the rule's."""
    if _subject_excluded(rule, agent_id):
        return False
    return rule_truth(rule, arguments)


def match_rule(rule: PolicyRule | Any, arguments: dict[str, Any], agent_id: str | None = None) -> bool:
    """Whether the rule applies. A restriction the engine cannot rule out applies; an allow it cannot confirm does not."""
    truth = match_truth(rule, arguments, agent_id)
    if truth is None:
        return _restrictive(rule)
    return truth


def _linked_approval_policy(rule: PolicyRule | Any) -> str | None:
    attributes = getattr(rule, "attributes", None)
    if not isinstance(attributes, dict):
        return None
    raw = attributes.get("approvalPolicyId") or attributes.get("approval_policy_id")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _result_for(rule: PolicyRule | Any, arguments: dict[str, Any]) -> EvaluationResult:
    return EvaluationResult(
        decision=normalize_decision(rule.decision),
        matched_rule=rule,
        policy_id=getattr(rule, "policy_id", None),
        matched_rule_id=getattr(rule, "id", None),
        reason=describe_reason(rule, arguments),
        approval_policy_id=_linked_approval_policy(rule),
    )


def evaluate_rules(
    rules: list[PolicyRule],
    arguments: dict[str, Any],
) -> EvaluationResult:
    if not rules:
        return EvaluationResult(decision=PolicyDecision.DENY, reason="No policy rule")

    # Restrictions the engine could not rule out, met before the first rule that clearly matched.
    unsure: list[PolicyRule | Any] = []
    for rule in sorted(rules, key=lambda item: item.position):
        truth = match_truth(rule, arguments)
        if truth is None and _restrictive(rule):
            unsure.append(rule)
            continue
        if truth is True:
            winner = max([rule, *unsure], key=lambda item: _RESTRICTIVENESS[normalize_decision(item.decision)])
            return _result_for(winner, arguments)
    return EvaluationResult(decision=PolicyDecision.DENY, reason="No matching policy rule")


_RESTRICTIVENESS = {
    PolicyDecision.DENY: 3,
    PolicyDecision.APPROVAL_REQUIRED: 2,
    PolicyDecision.ALLOW: 1,
}


def evaluate_graph_rules(
    rules: list[PolicyRule | Any],
    arguments: dict[str, Any],
    agent_id: str | None,
) -> EvaluationResult:
    """Most restrictive matching decision. A tool with rules but no match is denied."""
    if not rules:
        return EvaluationResult(decision=PolicyDecision.DENY, reason="No policy rule")
    truths = [(rule, match_truth(rule, arguments, agent_id)) for rule in rules]
    if not any(truth is True for _rule, truth in truths):
        return EvaluationResult(decision=PolicyDecision.DENY, reason="No matching policy rule")
    # A restriction the engine cannot rule out counts as matched, so it can only make the result stricter.
    matched = [rule for rule, truth in truths if truth is True or (truth is None and _restrictive(rule))]
    winner = max(matched, key=lambda rule: _RESTRICTIVENESS[normalize_decision(rule.decision)])
    return _result_for(winner, arguments)


def merge_results(results: list[EvaluationResult]) -> EvaluationResult:
    if not results:
        from app.core.config import get_settings

        if get_settings().policy_default_deny:
            return EvaluationResult(decision=PolicyDecision.DENY, reason="沒有適用的 policy")
        return EvaluationResult(decision=PolicyDecision.ALLOW)
    # Most restrictive wins. Between equally restrictive opinions, one that names
    # an approval policy beats one that does not, so a generic approval from
    # another policy cannot drop a stricter linked approval policy.
    return max(
        results,
        key=lambda item: (
            _RESTRICTIVENESS[item.decision],
            bool(item.approval_policy_id),
            bool(item.policy_id),
        ),
    )


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
        return await self.evaluate_named(
            session,
            organization_id=organization_id,
            agent=agent,
            tool_name=tool.name,
            arguments=arguments,
        )

    async def evaluate_named(
        self,
        session: AsyncSession,
        *,
        organization_id: str,
        agent: Agent,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> EvaluationResult:
        policies = await self._policies(session, organization_id)
        opinions: list[EvaluationResult] = []
        applicable: dict[str, Policy] = {}
        for policy in policies:
            if not _policy_applies(policy, agent):
                continue
            applicable[policy.id] = policy
            tool_rules = [rule for rule in policy.rules if rule.tool == tool_name]
            definition = policy.definition if isinstance(policy.definition, dict) else None
            if definition and definition.get("nodes"):
                if not tool_rules:
                    continue
                opinion = evaluate_graph_rules(tool_rules, arguments, agent.id)
                opinions.append(
                    replace(
                        opinion,
                        policy_id=opinion.policy_id or policy.id,
                        policy_name=policy.name,
                        policy_version=policy.version or 1,
                    )
                )
            elif tool_rules:
                # Each legacy policy reaches its own decision, first match by
                # position inside that policy only. Positions are never compared
                # across policies, so one policy's priority cannot pre-empt
                # another's rules; the merge below keeps the most restrictive.
                opinion = evaluate_rules(tool_rules, arguments)
                opinions.append(
                    replace(
                        opinion,
                        policy_id=opinion.policy_id or policy.id,
                        policy_name=policy.name,
                        policy_version=policy.version or 1,
                    )
                )
        if not opinions and applicable:
            opinions.append(EvaluationResult(decision=PolicyDecision.DENY, reason="No policy rule"))
        result = merge_results(opinions)
        if result.policy_id and result.policy_id in applicable and result.policy_name is None:
            policy = applicable[result.policy_id]
            result = replace(result, policy_version=policy.version or 1, policy_name=policy.name)
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

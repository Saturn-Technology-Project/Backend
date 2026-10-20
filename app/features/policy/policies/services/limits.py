"""Size limits for policy input.

The request schemas do not bound these fields, so the service checks them and
answers 400 instead of letting the database reject an oversized value with a
500. Numbers match the columns in models/policy.py where there is one.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, status

MAX_ID = 64
MAX_NAME = 255
MAX_ENVIRONMENT = 64
MAX_DESCRIPTION = 10_000
MAX_TOOL_NAME = 255
MAX_CONDITION_TEXT = 4_000
MAX_RULES = 500
MAX_CONDITIONS_PER_RULE = 32
MAX_FIELD = 128
MAX_VALUE_TEXT = 1_000
MAX_CONDITIONS_BYTES = 16_384
MAX_DEFINITION_BYTES = 1_000_000
MAX_AGENT_IDS = 500
MAX_PRIORITY = 100_000

# Paid model endpoints.
MAX_INSTRUCTIONS = 4_000
MAX_LLM_TOOLS = 200
LLM_USER_LIMIT = 20
LLM_ORG_LIMIT = 60
LLM_WINDOW_SECONDS = 3_600

# Canonical operator names, and the spellings accepted for each.
OPERATOR_ALIASES = {
    "gt": "gt",
    ">": "gt",
    "gte": "gte",
    "ge": "gte",
    ">=": "gte",
    "lt": "lt",
    "<": "lt",
    "lte": "lte",
    "le": "lte",
    "<=": "lte",
    "eq": "eq",
    "==": "eq",
    "ne": "ne",
    "!=": "ne",
}
OPERATORS = frozenset(OPERATOR_ALIASES.values())


def bad_request(message: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, message)


def json_size(value: Any) -> int:
    """Serialized size in bytes. Anything that cannot be serialized counts as too large."""
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return 1 << 62


def bounded_text(value: str | None, limit: int, label: str) -> str:
    text = (value or "").strip()
    if len(text) > limit:
        raise bad_request(f"{label} must be at most {limit} characters")
    return text


def canonical_operator(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    return OPERATOR_ALIASES.get(raw.strip().lower())


def scalar_value(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and len(value) <= MAX_VALUE_TEXT


def clean_condition(item: Any) -> dict[str, Any] | None:
    """Return the condition with its operator in canonical form, or None if it is not usable."""
    if not isinstance(item, dict):
        return None
    field = str(item.get("field") or "").strip()
    if not field or len(field) > MAX_FIELD:
        return None
    operator = canonical_operator(item.get("operator") if item.get("operator") is not None else "eq")
    if operator is None:
        return None
    value = item.get("value")
    if not scalar_value(value):
        return None
    return {"field": field, "operator": operator, "value": value}

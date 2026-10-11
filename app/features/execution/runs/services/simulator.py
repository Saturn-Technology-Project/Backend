from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlannedAction:
    tool: str
    arguments: dict[str, Any]


_AMOUNT = re.compile(
    r"(?:\$\s*(\d+(?:\.\d+)?)|(?:refund|amount|charge)\s+(?:of\s+)?(\d+(?:\.\d+)?))",
    re.IGNORECASE,
)


def extract_amount(text: str, default: float = 840.0) -> float:
    match = _AMOUNT.search(text)
    if not match:
        return default
    raw = match.group(1) or match.group(2)
    return float(raw)


def extract_customer(text: str, default: str = "Acme") -> str:
    if re.search(r"\bacme\b", text, re.IGNORECASE):
        return "Acme"
    match = re.search(r"\bfor\s+([A-Z][a-zA-Z]+)\b", text)
    if match:
        return match.group(1)
    match = re.search(r"\b([A-Z][a-zA-Z]+)'s\b", text)
    if match and match.group(1).lower() not in {"refund", "resolve", "update"}:
        return match.group(1)
    return default


class AgentSimulator:
    """Deterministic stand-in for the LLM agent runtime (tests / IN_PROCESS_RUNTIME)."""

    def plan(self, prompt: str) -> list[PlannedAction]:
        customer = extract_customer(prompt)
        amount = extract_amount(prompt)
        customer_id = "cus_001"
        return [
            PlannedAction("crm.get_customer", {"name": customer}),
            PlannedAction("billing.get_payment", {"customer_id": customer_id}),
            PlannedAction(
                "billing.refund",
                {"amount": amount, "customer_id": customer_id},
            ),
        ]


agent_simulator = AgentSimulator()

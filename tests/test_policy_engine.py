from types import SimpleNamespace

from app.models.enums import PolicyDecision
from app.services.policy_engine import evaluate_condition, evaluate_rules


def _rule(position: int, condition: str, decision: str) -> SimpleNamespace:
    return SimpleNamespace(
        position=position,
        condition=condition,
        decision=decision,
        policy_id="policy_refund",
    )


REFUND_RULES = [
    _rule(0, "amount <= 100", "allow"),
    _rule(1, "amount <= 1000", "approval"),
    _rule(2, "amount > 1000", "deny"),
]


def test_condition_comparisons():
    assert evaluate_condition("amount <= 100", {"amount": 50}) is True
    assert evaluate_condition("amount <= 100", {"amount": 840}) is False
    assert evaluate_condition("amount > 1000", {"amount": 5000}) is True
    assert evaluate_condition("true", {}) is True
    assert evaluate_condition("amount <= 100", {}) is False


def test_refund_policy_allow():
    result = evaluate_rules(REFUND_RULES, {"amount": 50})
    assert result.decision == PolicyDecision.ALLOW


def test_refund_policy_approval():
    result = evaluate_rules(REFUND_RULES, {"amount": 840})
    assert result.decision == PolicyDecision.APPROVAL_REQUIRED


def test_refund_policy_deny():
    result = evaluate_rules(REFUND_RULES, {"amount": 5000})
    assert result.decision == PolicyDecision.DENY


def test_and_conditions():
    assert (
        evaluate_condition("amount > 100 AND amount <= 1000", {"amount": 840}) is True
    )
    assert (
        evaluate_condition("amount > 100 AND amount <= 1000", {"amount": 50}) is False
    )


def test_structured_conditions():
    rule = SimpleNamespace(
        position=0,
        condition="true",
        conditions=[
            {"field": "amount", "operator": "gt", "value": 100},
            {"field": "amount", "operator": "lte", "value": 1000},
        ],
        decision="approval",
        policy_id="policy_refund",
        id="rule_02",
        tool="billing.refund",
    )
    result = evaluate_rules([rule], {"amount": 840})
    assert result.decision == PolicyDecision.APPROVAL_REQUIRED
    assert result.matched_rule_id == "rule_02"
    assert result.reason == "Refund amount exceeds $100"


def test_ungated_tool_allows_by_default():
    result = evaluate_rules([], {"customer_id": "cus_001"})
    assert result.decision == PolicyDecision.ALLOW


def test_gated_tool_denies_when_no_rule_matches():
    result = evaluate_rules(REFUND_RULES, {"customer_id": "cus_001"})
    assert result.decision == PolicyDecision.DENY

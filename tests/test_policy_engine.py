from types import SimpleNamespace

from app.core.enums import PolicyDecision
from app.features.policy.policies.services.policy_engine import evaluate_condition, evaluate_rules


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


def test_ungated_tool_denies_by_default():
    result = evaluate_rules([], {"customer_id": "cus_001"})
    assert result.decision == PolicyDecision.DENY
    assert result.reason == "No policy rule"


def test_gated_tool_denies_when_no_rule_matches():
    result = evaluate_rules(REFUND_RULES, {"customer_id": "cus_001"})
    assert result.decision == PolicyDecision.DENY


# Audit 2026-11-08: values an agent can steer must not slip past a restriction.

CAP_RULES = [
    _rule(0, "amount > 1000", "deny"),
    _rule(1, "true", "allow"),
]


def test_odd_spellings_of_a_large_amount_are_still_denied():
    for amount in ["+50000", " 50000", "50000 ", "$50000", "5e4", "50,000", float("nan"), float("inf"), True, None, [50000], {"v": 50000}]:
        assert evaluate_rules(CAP_RULES, {"amount": amount}).decision == PolicyDecision.DENY, repr(amount)


def test_numeric_strings_compare_as_numbers():
    assert evaluate_rules(CAP_RULES, {"amount": "0500000"}).decision == PolicyDecision.DENY
    assert evaluate_rules(CAP_RULES, {"amount": "9"}).decision == PolicyDecision.ALLOW
    assert evaluate_rules(CAP_RULES, {"amount": 9}).decision == PolicyDecision.ALLOW
    assert evaluate_rules(CAP_RULES, {"amount": "1000.5"}).decision == PolicyDecision.DENY
    assert evaluate_condition('amount > "1000"', {"amount": "9"}) is False


def test_a_missing_field_does_not_skip_a_restriction():
    assert evaluate_rules(CAP_RULES, {"other": 1}).decision == PolicyDecision.DENY


def test_an_unreadable_value_never_confirms_an_allow():
    rules = [_rule(0, "amount <= 100", "allow"), _rule(1, "amount > 100", "approval")]
    assert evaluate_rules(rules, {"amount": "+50"}).decision == PolicyDecision.DENY
    assert evaluate_rules(rules, {"amount": 50}).decision == PolicyDecision.ALLOW


def test_graph_rules_take_the_restriction_they_cannot_rule_out():
    from app.features.policy.policies.services.policy_engine import evaluate_graph_rules

    rules = [
        SimpleNamespace(position=0, condition="true", decision="allow", policy_id="p", expression={"op": "true"}),
        SimpleNamespace(
            position=1,
            condition="",
            decision="approval",
            policy_id="p",
            expression={"op": "cmp", "field": "amount", "operator": "gt", "value": 1000},
        ),
        SimpleNamespace(
            position=2,
            condition="",
            decision="deny",
            policy_id="p",
            expression={"op": "not", "args": [{"op": "cmp", "field": "currency", "operator": "eq", "value": "usd"}]},
        ),
    ]
    assert evaluate_graph_rules(rules, {"amount": 5, "currency": "usd"}, None).decision == PolicyDecision.ALLOW
    assert evaluate_graph_rules(rules, {"amount": " 5000", "currency": "usd"}, None).decision == PolicyDecision.APPROVAL_REQUIRED
    assert evaluate_graph_rules(rules, {"amount": 5, "currency": None}, None).decision == PolicyDecision.DENY
    # NFKC: a fullwidth spelling is the same text.
    assert evaluate_graph_rules(rules, {"amount": 5, "currency": "ｕｓｄ"}, None).decision == PolicyDecision.ALLOW


def test_ordering_conditions_need_numbers():
    from app.features.policy.policies.services.limits import clean_condition

    assert clean_condition({"field": "amount", "operator": "gt", "value": "1000"}) == {"field": "amount", "operator": "gt", "value": 1000}
    assert clean_condition({"field": "amount", "operator": "gt", "value": "2.5"})["value"] == 2.5
    for value in ["$1000", "1,000", " 1000", True, None, "abc"]:
        assert clean_condition({"field": "amount", "operator": "gt", "value": value}) is None, repr(value)
    assert clean_condition({"field": "env", "operator": "eq", "value": "prod"})["value"] == "prod"

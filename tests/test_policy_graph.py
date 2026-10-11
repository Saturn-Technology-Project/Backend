from types import SimpleNamespace

from app.core.enums import PolicyDecision
from app.features.policy.policies.services.policy_engine import evaluate_graph_rules, merge_results, EvaluationResult
from app.features.policy.policies.services.policy_graph import compile_definition, definition_from_analyses, diff_definitions, validate_definition
from app.features.policy.policies.services.policy_llm import _guard


def _chain(tool: str, decision: str, conditions: list[dict] | None = None) -> dict:
    return definition_from_analyses(
        "set_ops",
        [
            {
                "tool": tool,
                "status": "analyzed",
                "operation": "read",
                "resource": "file",
                "risk": "low",
                "sideEffects": "none",
                "decision": decision,
                "conditions": conditions or [],
                "rationale": "schema says read",
            }
        ],
    )


def test_compiles_tool_to_decision():
    rules, errors = compile_definition(_chain("repo.read", "allow"))
    assert errors == []
    assert rules[0]["tool"] == "repo.read"
    assert rules[0]["decision"] == "allow"
    assert rules[0]["expression"] == {"op": "true"}


def test_chained_conditions_are_anded():
    definition = {
        "nodes": [
            {"id": "tool_58", "type": "tool", "data": {"tool": "modify_order"}},
            {"id": "condition_58_0", "type": "condition", "data": {"field": "cash_size", "operator": "lt", "value": 10000}},
            {"id": "condition_58_1", "type": "condition", "data": {"field": "size", "operator": "lt", "value": 100}},
            {"id": "decision_58", "type": "decision", "data": {"decision": "allow"}},
        ],
        "edges": [
            {"id": "a", "source": "tool_58", "target": "condition_58_0"},
            {"id": "b", "source": "condition_58_0", "target": "condition_58_1"},
            {"id": "c", "source": "condition_58_1", "target": "decision_58"},
        ],
    }
    rules, errors = compile_definition(definition)
    assert errors == []
    rule = SimpleNamespace(**rules[0], condition="true", conditions=None, position=0, policy_id="p")
    assert evaluate_graph_rules([rule], {"size": 50, "cash_size": 50000}, None).decision == PolicyDecision.DENY
    assert evaluate_graph_rules([rule], {"size": 50, "cash_size": 100}, None).decision == PolicyDecision.ALLOW


def test_two_conditions_must_both_match():
    definition = _chain(
        "modify_order",
        "allow",
        [
            {"field": "cash_size", "operator": "lt", "value": 10000},
            {"field": "size", "operator": "lt", "value": 100},
        ],
    )
    rules, errors = compile_definition(definition)
    assert errors == []
    assert rules[0]["expression"]["op"] == "and"
    rule = SimpleNamespace(**rules[0], condition="true", conditions=None, position=0, policy_id="p")
    assert evaluate_graph_rules([rule], {"size": 50, "cash_size": 50000}, None).decision == PolicyDecision.DENY
    assert evaluate_graph_rules([rule], {"size": 50, "cash_size": 100}, None).decision == PolicyDecision.ALLOW


def test_or_condition_matches_either_side():
    definition = {
        "nodes": [
            {"id": "tool", "type": "tool", "position": {"x": 0, "y": 0}, "data": {"tool": "billing.refund"}},
            {"id": "low", "type": "condition", "position": {"x": 1, "y": 0}, "data": {"field": "amount", "operator": "lt", "value": 10}},
            {"id": "high", "type": "condition", "position": {"x": 1, "y": 1}, "data": {"field": "amount", "operator": "gt", "value": 100}},
            {"id": "either", "type": "logic", "position": {"x": 2, "y": 0}, "data": {"op": "or"}},
            {"id": "decision", "type": "decision", "position": {"x": 3, "y": 0}, "data": {"decision": "approval"}},
        ],
        "edges": [
            {"id": "a", "source": "tool", "target": "either"},
            {"id": "b", "source": "low", "target": "either"},
            {"id": "c", "source": "high", "target": "either"},
            {"id": "d", "source": "either", "target": "decision"},
        ],
    }
    rules, errors = compile_definition(definition)
    assert errors == []
    rule = SimpleNamespace(**rules[0], condition="true", conditions=None, position=0, policy_id="p")
    assert evaluate_graph_rules([rule], {"amount": 5}, None).decision == PolicyDecision.APPROVAL_REQUIRED
    assert evaluate_graph_rules([rule], {"amount": 200}, None).decision == PolicyDecision.APPROVAL_REQUIRED
    assert evaluate_graph_rules([rule], {"amount": 50}, None).decision == PolicyDecision.DENY


def test_most_restrictive_decision_wins():
    allow = SimpleNamespace(
        id="a", policy_id="p", tool="place_order", condition="true", conditions=None,
        expression={"op": "true"}, decision="allow", attributes={}, position=0,
    )
    deny = SimpleNamespace(
        id="d", policy_id="p", tool="place_order", condition="true", conditions=None,
        expression={"op": "true"}, decision="deny", attributes={}, position=1,
    )
    result = evaluate_graph_rules([allow, deny], {}, None)
    assert result.decision == PolicyDecision.DENY
    assert result.matched_rule_id == "d"


def test_deny_from_one_policy_overrides_allow_from_another():
    merged = merge_results(
        [
            EvaluationResult(decision=PolicyDecision.ALLOW, policy_id="open"),
            EvaluationResult(decision=PolicyDecision.DENY, policy_id="org", reason="org deny"),
        ]
    )
    assert merged.decision == PolicyDecision.DENY
    assert merged.policy_id == "org"


def test_unclassified_proposal_cannot_allow():
    guarded = _guard(
        {"name": "create_order_instruction"},
        {"tool": "create_order_instruction", "status": "insufficient", "decision": "allow", "rationale": "name looks safe"},
    )
    assert guarded["decision"] == "deny"
    assert guarded["status"] == "insufficient"


def test_validation_reports_missing_tool():
    report = validate_definition(_chain("repo.read", "allow"), ["repo.read", "repo.delete"])
    assert report["ok"] is False
    assert any(item["tool"] == "repo.delete" for item in report["issues"])


def test_diff_ignores_position_and_reports_decision_change():
    before = _chain("repo.read", "allow")
    after = _chain("repo.read", "deny")
    after["nodes"][0]["position"] = {"x": 400, "y": 20}
    changes = diff_definitions(before, after)
    assert len(changes) == 1
    assert changes[0]["kind"] == "changed"
    assert changes[0]["path"] == "nodes/decision_0"
    assert "allow" in changes[0]["summary"]
    assert "deny" in changes[0]["summary"]


def test_subject_limits_the_rule():
    definition = _chain("repo.read", "allow")
    definition["nodes"].insert(
        0,
        {"id": "subject", "type": "subject", "position": {"x": -100, "y": 0}, "data": {"agentIds": ["agent_a"]}},
    )
    definition["edges"].append({"id": "subject_edge", "source": "subject", "target": "tool_0"})
    rules, errors = compile_definition(definition)
    assert errors == []
    rule = SimpleNamespace(**rules[0], condition="true", conditions=None, position=0, policy_id="p")
    assert evaluate_graph_rules([rule], {}, "agent_a").decision == PolicyDecision.ALLOW
    assert evaluate_graph_rules([rule], {}, "agent_b").decision == PolicyDecision.DENY

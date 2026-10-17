from app.features.mcp.saturn.services.agent_messages import (
    APPROVAL_ARGS_MISMATCH,
    NO_APPLICABLE_POLICY,
    POLICY_DENIED,
    deny_error,
    mismatch_error,
)


def test_deny_and_missing_policy_use_english_placeholders():
    denied = deny_error(1, reason="amount > 10", policy_id="policy_refund")
    assert denied["error"]["data"]["code"] == "policy_denied"
    assert denied["error"]["message"] == POLICY_DENIED
    assert denied["error"]["data"]["policy_id"] == "policy_refund"
    assert "amount" not in denied["error"]["message"]

    missing = deny_error(2, reason="沒有適用的 policy", policy_id=None)
    assert missing["error"]["data"]["code"] == "no_applicable_policy"
    assert missing["error"]["message"] == NO_APPLICABLE_POLICY
    assert "policy_id" not in missing["error"]["data"]

    unmatched = deny_error(3, reason="No policy rule", policy_id="policy_lookup")
    assert unmatched["error"]["data"]["code"] == "no_applicable_policy"


def test_argument_mismatch_does_not_include_the_arguments():
    error = mismatch_error(4)
    assert error["error"]["data"]["code"] == "approval_args_mismatch"
    assert error["error"]["message"] == APPROVAL_ARGS_MISMATCH
    assert "approval_url" not in error["error"]["data"]

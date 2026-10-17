"""English stand-in copy for agent-facing tool errors. Replace these strings later."""

from __future__ import annotations

from typing import Any

POLICY_DENIED = "This tool call was denied by policy."
NO_APPLICABLE_POLICY = "No policy applies to this tool call."
APPROVAL_ARGS_MISMATCH = "These arguments do not match the approved call."
_DASHBOARD = "https://dashboard.saturn-core.com"


def approval_pending(tool_name: str, run_id: str) -> str:
    return (
        "This tool call is waiting for approval. "
        f"After it is approved, call {tool_name} again with the same arguments "
        f'and set saturnRunId to "{run_id}".'
    )


def approval_url(approval_id: str) -> str:
    return f"{_DASHBOARD}/approvals/{approval_id}"


def _error(request_id: Any, rpc_code: int, code: str, message: str, *, policy_id: str | None, approval: str | None) -> dict[str, Any]:
    data: dict[str, Any] = {"code": code, "message": message}
    if policy_id:
        data["policy_id"] = policy_id
    if approval:
        data["approval_url"] = approval
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": rpc_code, "message": message, "data": data}}


def deny_error(request_id: Any, *, reason: str | None, policy_id: str | None) -> dict[str, Any]:
    code = "no_applicable_policy" if (reason or "") in {"沒有適用的 policy", "No policy rule"} else "policy_denied"
    message = NO_APPLICABLE_POLICY if code == "no_applicable_policy" else POLICY_DENIED
    return _error(request_id, -32004, code, message, policy_id=policy_id, approval=None)


def mismatch_error(request_id: Any, *, policy_id: str | None = None) -> dict[str, Any]:
    return _error(request_id, -32004, "approval_args_mismatch", APPROVAL_ARGS_MISMATCH, policy_id=policy_id, approval=None)


def pending_fields(message: str, approval_id: str, policy_id: str | None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "code": "approval_pending",
        "message": message,
        "approval_url": approval_url(approval_id),
    }
    if policy_id:
        data["policy_id"] = policy_id
    return data

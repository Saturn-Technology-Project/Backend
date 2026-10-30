from __future__ import annotations

from fastapi import HTTPException, status

PLANS = {"free", "pro", "team", "enterprise"}
_ALIASES = {"trial": "free", "starter": "pro"}


def canonical_plan(plan: str | None, *, strict: bool = False) -> str:
    cleaned = _ALIASES.get((plan or "").strip().lower(), (plan or "").strip().lower())
    if cleaned in PLANS:
        return cleaned
    if strict:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown plan")
    return "free"


def kind_for_plan(plan: str) -> str:
    return "organization" if canonical_plan(plan) in {"team", "enterprise"} else "personal"


PRO_REQUIRED = "This feature requires Pro"
GROUPS_REQUIRED = "Groups and multi-member approval require Team"


# Saturn is free: every plan gets every feature. The plan names and these checks stay so that
# stored plans keep parsing and a paid tier can come back by changing these two functions.
def allows_pro(plan: str | None) -> bool:
    return canonical_plan(plan) in PLANS


def allows_groups(plan: str | None) -> bool:
    return canonical_plan(plan) in PLANS


def allows_saturn_cloud(plan: str | None) -> bool:
    """Every plan, including Free, can use the MCP gateway."""
    return canonical_plan(plan) in PLANS


def tool_set_limit(plan: str | None) -> int | None:
    """Tool sets are unlimited on every plan."""
    return None


def audit_retention_days(plan: str | None) -> int | None:
    """Audit history is unlimited on every plan. None means no day cutoff."""
    return None


def entitlements(plan: str | None) -> dict:
    pro = allows_pro(plan)
    return {
        "gateway": allows_saturn_cloud(plan),
        "tool_set_limit": tool_set_limit(plan),
        "policies": pro,
        "webhooks": pro,
        "groups": allows_groups(plan),
        "audit": "full",
        "audit_days": audit_retention_days(plan),
    }


def profile_identity(
    user_id: str,
    organization_id: str,
    kind: str | None,
    plan: str | None = None,
) -> str:
    resolved = (kind or "").strip().lower()
    if resolved not in {"personal", "organization"}:
        resolved = kind_for_plan(plan)
    if resolved == "personal":
        return user_id
    return f"{organization_id}:{user_id}"

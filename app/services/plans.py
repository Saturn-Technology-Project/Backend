from __future__ import annotations

from fastapi import HTTPException, status

PLANS = {"free", "pro", "enterprise"}
_ALIASES = {"trial": "free", "starter": "pro"}


def canonical_plan(plan: str | None, *, strict: bool = False) -> str:
    cleaned = _ALIASES.get((plan or "").strip().lower(), (plan or "").strip().lower())
    if cleaned in PLANS:
        return cleaned
    if strict:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown plan")
    return "free"


def kind_for_plan(plan: str) -> str:
    return "organization" if canonical_plan(plan) == "enterprise" else "personal"


def allows_saturn_cloud(plan: str | None) -> bool:
    return canonical_plan(plan) in {"pro", "enterprise"}


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

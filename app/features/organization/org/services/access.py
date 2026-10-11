from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

RESOURCES = ("agents", "policies", "approvals", "team")
LEVELS = ("none", "read", "write")
ROLES = ("admin", "operator", "viewer", "custom")

_RANK = {"none": 0, "read": 1, "write": 2}

ROLE_PERMISSIONS: dict[str, dict[str, str]] = {
    "admin": {
        "agents": "write",
        "policies": "write",
        "approvals": "write",
        "team": "write",
    },
    "operator": {
        "agents": "write",
        "policies": "read",
        "approvals": "write",
        "team": "none",
    },
    "viewer": {
        "agents": "read",
        "policies": "read",
        "approvals": "read",
        "team": "none",
    },
}


def empty_permissions() -> dict[str, str]:
    return {resource: "none" for resource in RESOURCES}


def permissions_for_role(role: str) -> dict[str, str]:
    if role == "custom":
        return empty_permissions()
    preset = ROLE_PERMISSIONS.get(role)
    if preset is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown role: {role}")
    return dict(preset)


def normalize_permissions(raw: Any | None) -> dict[str, str]:
    source = raw if isinstance(raw, dict) else {}
    normalized = empty_permissions()
    for resource in RESOURCES:
        level = str(source.get(resource, "none")).strip().lower()
        if level not in _RANK:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Invalid access for {resource}: {level}",
            )
        normalized[resource] = level
    return normalized


def resolve_permissions(role: str, override: Any | None = None) -> tuple[str, dict[str, str]]:
    cleaned_role = (role or "operator").strip().lower()
    if cleaned_role not in ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown role: {cleaned_role}")
    if override is not None:
        perms = normalize_permissions(override)
        for name, preset in ROLE_PERMISSIONS.items():
            if perms == preset:
                return name, perms
        return "custom", perms
    if cleaned_role == "custom":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "custom role requires permissions")
    return cleaned_role, permissions_for_role(cleaned_role)


def allows(permissions: Any | None, resource: str, level: str) -> bool:
    if resource not in RESOURCES:
        return False
    needed = _RANK.get(level, 99)
    current = _RANK.get(
        str((permissions or {}).get(resource, "none")).strip().lower(),
        0,
    )
    return current >= needed


def is_org_admin(permissions: Any | None) -> bool:
    return allows(permissions, "team", "write")


def may_edit_agent(employee: Any, agent_id: str) -> bool:
    if employee is None:
        return False
    permissions = getattr(employee, "permissions", None)
    if allows(permissions, "team", "write"):
        return True
    if not allows(permissions, "agents", "write"):
        return False
    edits = getattr(employee, "agent_edit_ids", None)
    if edits is None:
        return True
    return agent_id in set(edits)


def permission_catalog() -> dict[str, Any]:
    return {
        "resources": [
            {
                "id": "agents",
                "label": "Agents",
                "actions": ["read", "write"],
            },
            {
                "id": "policies",
                "label": "Policies",
                "actions": ["read", "write"],
            },
            {
                "id": "approvals",
                "label": "Approvals",
                "actions": ["read", "write"],
            },
            {
                "id": "team",
                "label": "Team",
                "actions": ["read", "write"],
            },
        ],
        "roles": [
            {"id": role, "permissions": perms}
            for role, perms in ROLE_PERMISSIONS.items()
        ],
        "levels": list(LEVELS),
    }

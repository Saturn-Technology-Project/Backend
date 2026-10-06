from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from app.models import Agent, Wallet

RESPONSE_STYLES = ("concise", "professional", "detailed")
AUTONOMY = ("suggest_only", "execute_when_allowed", "fully_autonomous")
PROVIDERS = ("openai", "google", "deepseek", "anthropic", "custom")
PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
    "anthropic": "https://api.anthropic.com",
    "deepseek": "https://api.deepseek.com",
}
CUSTOMER_KEYS = {
    "instructions",
    "response_style",
    "autonomy",
    "tools",
    "knowledge",
    "provider",
    "model",
    "base_url",
    "temperature",
    "max_output",
    "model_connection_id",
}
ADMIN_KEYS = {
    "environment",
    "region",
    "max_concurrent_runs",
    "max_tool_calls",
    "max_runtime_minutes",
}
GENERIC_DUTY = (
    "Your responsibility is to complete the operator's request using your assigned tools."
)


def default_instructions(name: str = "", description: str = "") -> str:
    who = (name or "").strip() or "a Saturn agent"
    duty = (description or "").strip() or GENERIC_DUTY
    return (
        f"You are {who}.\n\n"
        f"{duty}\n\n"
        "Do not perform actions outside your assigned role.\n\n"
        "When a tool result is enough, answer in one short sentence."
    )


def resolved_instructions(agent: Agent) -> str:
    text = str((agent.settings or {}).get("instructions") or "").strip()
    return text or default_instructions(agent.name, agent.description)


def infer_provider(model: str) -> str:
    name = (model or "").lower()
    if name.startswith("gpt") or "openai" in name:
        return "openai"
    if "gemini" in name or name.startswith("google"):
        return "google"
    if "deepseek" in name:
        return "deepseek"
    if name.startswith("claude") or "anthropic" in name:
        return "anthropic"
    return "custom"


def _url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not (text.startswith("https://") or text.startswith("http://")):
        return ""
    return text.rstrip("/")


def _one(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def _float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _knowledge(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        ident = str(raw.get("id") or "").strip() or f"ks_{index}"
        items.append(
            {
                "id": ident,
                "name": name,
                "kind": "document",
                "enabled": bool(raw.get("enabled", True)),
            }
        )
    return items


def _tools(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    names: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = str(item).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def resolved_base_url(provider: str, override: Any = "") -> str:
    custom = _url(override)
    if provider == "custom":
        return custom
    return custom or PROVIDER_BASE_URLS.get(provider, "")


def public_settings(agent: Agent) -> dict[str, Any]:
    raw = dict(agent.settings or {})
    model = str(raw.get("model") or agent.model or "claude-sonnet")
    provider = _one(raw.get("provider"), PROVIDERS, infer_provider(model))
    return {
        "instructions": resolved_instructions(agent),
        "response_style": _one(raw.get("response_style"), RESPONSE_STYLES, "professional"),
        "autonomy": _one(raw.get("autonomy"), AUTONOMY, "execute_when_allowed"),
        "tools": _tools(raw.get("tools")) if "tools" in raw else None,
        "knowledge": _knowledge(raw.get("knowledge")),
        "provider": provider,
        "model": model,
        "base_url": resolved_base_url(provider, raw.get("base_url")),
        "temperature": _float(raw.get("temperature"), 0.2, 0, 2),
        "max_output": _int(raw.get("max_output"), 4000, 1, 128_000),
        "model_connection_id": str(raw.get("model_connection_id") or ""),
        "environment": str(raw.get("environment") or ""),
        "region": str(raw.get("region") or ""),
        "max_concurrent_runs": _int(raw.get("max_concurrent_runs"), 5, 1, 1_000),
        "max_tool_calls": _int(raw.get("max_tool_calls"), 50, 1, 10_000),
        "max_runtime_minutes": _int(raw.get("max_runtime_minutes"), 30, 1, 24 * 60),
    }


def merge_settings(current: dict[str, Any] | None, patch: dict[str, Any], *, admin: bool) -> dict[str, Any]:
    merged = dict(current or {})
    for key, value in patch.items():
        if key in ADMIN_KEYS and not admin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
        if key in CUSTOMER_KEYS or (admin and key in ADMIN_KEYS):
            merged[key] = value
    return merged


def assigned_tool_names(settings: dict[str, Any] | None, catalog: list[str]) -> list[str]:
    assigned = _tools((settings or {}).get("tools")) if settings and "tools" in settings else None
    if assigned is None:
        return list(catalog)
    allowed = set(assigned)
    return [name for name in catalog if name in allowed]


def system_prompt(agent: Agent) -> str:
    return resolved_instructions(agent)


def command_payload(agent: Agent, wallet: Wallet | None = None) -> dict[str, Any]:
    body = public_settings(agent)
    body["runtime_id"] = agent.runtime_id
    body["agent_id"] = agent.id
    if wallet is not None:
        body["wallet"] = {
            "id": wallet.id,
            "daily_spend": str(wallet.daily_spend_limit),
            "per_tx": str(wallet.per_tx_limit),
            "asset": wallet.asset,
        }
    return body

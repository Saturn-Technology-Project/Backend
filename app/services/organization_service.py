from __future__ import annotations

from typing import Any

from app.models.organization import Organization
from app.schemas import OrgAddonRead, OrganizationRead
from app.services.plans import canonical_plan, kind_for_plan


def public_addons(raw: Any) -> list[OrgAddonRead]:
    if not isinstance(raw, list):
        return []
    items: list[OrgAddonRead] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        ident = str(entry.get("id") or "").strip() or f"addon_{index}"
        if ident in seen:
            continue
        seen.add(ident)
        href = str(entry.get("href") or "").strip() or None
        items.append(OrgAddonRead(id=ident, name=name, href=href))
    return items


def serialize_organization(org: Organization) -> OrganizationRead:
    return OrganizationRead(
        id=org.id,
        name=org.name,
        plan=canonical_plan(org.plan),
        kind=org.kind if org.kind in {"personal", "organization"} else kind_for_plan(org.plan),
        addons=public_addons(org.addons),
    )

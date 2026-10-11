from __future__ import annotations

from app.features.organization.org.models.organization import Organization
from app.schemas import EntitlementsRead, OrganizationRead
from app.features.organization.org.services.plans import canonical_plan, entitlements, kind_for_plan


def serialize_organization(org: Organization) -> OrganizationRead:
    return OrganizationRead(
        id=org.id,
        name=org.name,
        plan=canonical_plan(org.plan),
        kind=org.kind if org.kind in {"personal", "organization"} else kind_for_plan(org.plan),
        entitlements=EntitlementsRead.model_validate(entitlements(org.plan)),
        photo=org.photo or None,
    )

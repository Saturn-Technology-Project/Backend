from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Organization, User
from app.services.auth_service import auth_service
from app.services.mapping_service import mapping_service

settings = get_settings()


async def seed_if_empty(session: AsyncSession) -> None:
    """Bootstrap an empty control plane: org + platform admin, no catalog."""
    existing = await session.get(Organization, settings.default_org_id)
    if existing is None:
        session.add(
            Organization(
                id=settings.default_org_id,
                name="Saturn",
                status="active",
                plan="enterprise",
                kind="organization",
                deployment_mode="cloud",
                runtime_region="asia-east1",
                runtime_version="0.1.0",
            )
        )
        await session.flush()

    await auth_service.ensure_seed_user(session)
    user = await session.get(User, settings.default_actor_id)
    if user is not None:
        await mapping_service.ensure_bootstrap_employee(session, user)

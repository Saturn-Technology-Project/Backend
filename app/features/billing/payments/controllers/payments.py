"""Reserved for Stripe. This route does not charge or store payments yet."""

from fastapi import APIRouter

from app.features.identity.auth.controllers.deps import CurrentUser

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("")
async def list_payments(_user: CurrentUser):
    return []

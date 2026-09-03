from fastapi import APIRouter

from app.core.config import get_settings
from app.schemas import X402SettleResponse, X402VerifyRequest, X402VerifyResponse
from app.services.x402 import settle_payment, verify_payment

router = APIRouter(prefix="/x402", tags=["x402"])
settings = get_settings()


@router.get("/supported")
async def supported():
    return {"kinds": [{"scheme": "exact", "network": settings.x402_network}]}


@router.post("/verify", response_model=X402VerifyResponse)
async def verify(body: X402VerifyRequest):
    result = verify_payment(body.paymentPayload, body.paymentRequirements)
    return X402VerifyResponse(
        isValid=bool(result["isValid"]),
        invalidReason=result.get("invalidReason"),
    )


@router.post("/settle", response_model=X402SettleResponse)
async def settle(body: X402VerifyRequest):
    result = settle_payment(body.paymentPayload, body.paymentRequirements)
    return X402SettleResponse(
        success=bool(result["success"]),
        transaction=result.get("transaction"),
        network=result.get("network"),
        errorReason=result.get("errorReason"),
    )

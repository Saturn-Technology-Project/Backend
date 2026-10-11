from __future__ import annotations

import httpx
from fastapi import HTTPException, status

from app.core.config import get_settings


async def send_reset_code(email: str, code: str) -> None:
    settings = get_settings()
    if not settings.resend_api_key or not settings.resend_from:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Email delivery is not configured")
    text = (
        f"你的 Saturn 驗證碼是 {code}，10 分鐘內有效。\n"
        f"Your Saturn verification code is {code}. It expires in 10 minutes.\n"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={
                    "from": settings.resend_from,
                    "to": [email],
                    "subject": "Saturn 驗證碼",
                    "text": text,
                },
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not send email") from exc
    if response.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not send email")

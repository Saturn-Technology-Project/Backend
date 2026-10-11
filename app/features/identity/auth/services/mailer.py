from __future__ import annotations

from html import escape

import httpx
from fastapi import HTTPException, status

from app.core.config import get_settings

_COPY = {
    "signup": {
        "zh": {
            "subject": "{code} 是你的 Saturn 帳號確認碼",
            "title": "確認你的帳號",
            "body": "輸入下面的驗證碼，完成建立帳號。這組碼 10 分鐘內有效。",
            "ignore": "如果你沒有提出這個要求，可以忽略這封信。",
        },
        "en": {
            "subject": "{code} is your Saturn confirmation code",
            "title": "Confirm your account",
            "body": "Enter this code to finish creating your account. It expires in 10 minutes.",
            "ignore": "If you didn't request this, you can ignore this email.",
        },
    },
    "login": {
        "zh": {
            "subject": "{code} 是你的 Saturn 登入驗證碼",
            "title": "登入 Saturn",
            "body": "輸入下面的驗證碼以完成登入。這組碼 10 分鐘內有效。",
            "ignore": "如果你沒有提出這個要求，可以忽略這封信。",
        },
        "en": {
            "subject": "{code} is your Saturn sign-in code",
            "title": "Sign in to Saturn",
            "body": "Enter this code to finish signing in. It expires in 10 minutes.",
            "ignore": "If you didn't request this, you can ignore this email.",
        },
    },
    "reset": {
        "zh": {
            "subject": "{code} 是你的 Saturn 密碼重設碼",
            "title": "重設密碼",
            "body": "輸入下面的驗證碼以設定新密碼。這組碼 10 分鐘內有效。",
            "ignore": "如果你沒有提出這個要求，可以忽略這封信。",
        },
        "en": {
            "subject": "{code} is your Saturn password reset code",
            "title": "Reset your password",
            "body": "Enter this code to choose a new password. It expires in 10 minutes.",
            "ignore": "If you didn't request this, you can ignore this email.",
        },
    },
}


def mail_locale(value: str | None) -> str:
    token = (value or "").split(",")[0].split(";")[0].strip().lower().replace("_", "-")
    if token.startswith("en"):
        return "en"
    return "zh"


async def send_reset_code(email: str, code: str, locale: str | None = None) -> None:
    await _send(email, code, "reset", mail_locale(locale))


async def send_signup_code(email: str, code: str, locale: str | None = None) -> None:
    await _send(email, code, "signup", mail_locale(locale))


async def send_login_code(email: str, code: str, locale: str | None = None) -> None:
    await _send(email, code, "login", mail_locale(locale))


def _code_message(code: str, kind: str, locale: str) -> tuple[str, str, str]:
    copy = _COPY[kind][locale]
    subject = copy["subject"].format(code=code)
    text = f"{copy['title']}\n\n{code}\n\n{copy['body']}\n\n{copy['ignore']}\n\nSaturn\n"
    lang = "zh-Hant" if locale == "zh" else "en"
    html = f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(copy["title"])}</title>
</head>
<body style="margin:0;padding:0;background:#f7f7f5;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">{escape(copy["body"])}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f7f7f5;">
    <tr>
      <td align="center" style="padding:40px 16px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:440px;background:#ffffff;border:1px solid #e6e6e1;border-radius:12px;">
          <tr>
            <td style="padding:32px 32px 28px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111110;">
              <div style="font-size:15px;font-weight:600;letter-spacing:-0.02em;">Saturn</div>
              <h1 style="margin:28px 0 0;font-size:22px;line-height:1.3;font-weight:600;letter-spacing:-0.02em;">{escape(copy["title"])}</h1>
              <p style="margin:22px 0 0;font-size:14px;line-height:1.6;">{escape(copy["body"])}</p>
              <div style="margin:24px 0 0;padding:18px 12px;background:#f7f7f5;border-radius:10px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:32px;line-height:1;letter-spacing:0.28em;text-align:center;">{escape(code)}</div>
              <p style="margin:24px 0 0;font-size:12px;line-height:1.6;color:#8a8a84;">{escape(copy["ignore"])}</p>
            </td>
          </tr>
        </table>
        <p style="margin:16px 0 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:12px;line-height:1.5;color:#8a8a84;">Saturn</p>
      </td>
    </tr>
  </table>
</body>
</html>"""
    return subject, text, html


async def _send(email: str, code: str, kind: str, locale: str) -> None:
    settings = get_settings()
    if not settings.resend_api_key or not settings.resend_from:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Email delivery is not configured")
    subject, text, html = _code_message(code, kind, locale)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={
                    "from": settings.resend_from,
                    "to": [email],
                    "subject": subject,
                    "text": text,
                    "html": html,
                },
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not send email") from exc
    if response.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not send email")

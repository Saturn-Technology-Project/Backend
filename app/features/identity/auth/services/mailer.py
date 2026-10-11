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


def normalize_locale(value: str | None) -> str:
    token = (value or "").split(",")[0].split(";")[0].strip().lower().replace("_", "-")
    if token.startswith("en"):
        return "en-US"
    return "zh-Hant"


def mail_locale(value: str | None) -> str:
    return normalize_locale(value)


def is_english(value: str | None) -> bool:
    return normalize_locale(value) == "en-US"


async def send_reset_code(email: str, code: str, locale: str | None = None) -> None:
    await _send(email, code, "reset", mail_locale(locale))


async def send_signup_code(email: str, code: str, locale: str | None = None) -> None:
    await _send(email, code, "signup", mail_locale(locale))


async def send_login_code(email: str, code: str, locale: str | None = None) -> None:
    await _send(email, code, "login", mail_locale(locale))


def console_origin() -> str:
    for origin in get_settings().cors_origins.split(","):
        cleaned = origin.strip().rstrip("/")
        if cleaned.startswith("https://"):
            return cleaned
    return "https://dashboard.saturn-core.com"


async def send_notice(email: str, *, kind: str, tool: str, agent: str, link: str, locale: str | None = None) -> None:
    settings = get_settings()
    if not settings.resend_api_key or not settings.resend_from:
        return
    subject, text, html = _notice_message(kind, tool, agent, link, normalize_locale(locale))
    try:
        async with httpx.AsyncClient(timeout=8) as client:
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
    except httpx.HTTPError:
        return
    if response.status_code >= 400:
        return


def _copy_language(locale: str) -> str:
    return "en" if is_english(locale) else "zh"


def _card(lang: str, title: str, inner: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="{escape(lang)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
</head>
<body style="margin:0;padding:0;background:#f7f7f5;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">{escape(title)}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f7f7f5;">
    <tr>
      <td align="center" style="padding:40px 16px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:440px;background:#ffffff;border:1px solid #e6e6e1;border-radius:12px;">
          <tr>
            <td style="padding:32px 32px 28px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111110;">
              <div style="font-size:15px;font-weight:600;letter-spacing:-0.02em;">Saturn</div>
              {inner}
            </td>
          </tr>
        </table>
        <p style="margin:16px 0 0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:12px;line-height:1.5;color:#8a8a84;">Saturn</p>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _code_message(code: str, kind: str, locale: str) -> tuple[str, str, str]:
    copy = _COPY[kind][_copy_language(locale)]
    subject = copy["subject"].format(code=code)
    text = f"{copy['title']}\n\n{code}\n\n{copy['body']}\n\n{copy['ignore']}\n\nSaturn\n"
    code_block = (
        f'<div style="margin:24px 0 0;padding:18px 12px;background:#f7f7f5;border-radius:10px;'
        f'font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:32px;line-height:1;'
        f'letter-spacing:0.28em;text-align:center;">{escape(code)}</div>'
    )
    inner = f"""<h1 style="margin:28px 0 0;font-size:22px;line-height:1.3;font-weight:600;letter-spacing:-0.02em;">{escape(copy["title"])}</h1>
              <p style="margin:22px 0 0;font-size:14px;line-height:1.6;">{escape(copy["body"])}</p>
              {code_block}
              <p style="margin:24px 0 0;font-size:12px;line-height:1.6;color:#8a8a84;">{escape(copy["ignore"])}</p>"""
    return subject, text, _card(normalize_locale(locale), copy["title"], inner)


_NOTICE = {
    "approval": {
        "zh": {
            "subject": "Saturn：{tool} 需要核准",
            "title": "需要核准",
            "body": "{who}呼叫 {tool}。這次操作要先經過核准。",
            "empty": "有一個工具呼叫需要核准。",
            "link": "查看稽核紀錄",
        },
        "en": {
            "subject": "Saturn: {tool} needs approval",
            "title": "Approval needed",
            "body": "{who} called {tool}. This action waits for approval.",
            "empty": "A tool call is waiting for approval.",
            "link": "View the audit log",
        },
    },
    "execute": {
        "zh": {
            "subject": "Saturn：{tool} 已執行",
            "title": "已執行",
            "body": "{who}已執行 {tool}。",
            "empty": "有一個工具呼叫已經執行。",
            "link": "查看稽核紀錄",
        },
        "en": {
            "subject": "Saturn: {tool} ran",
            "title": "Executed",
            "body": "{who} ran {tool}.",
            "empty": "A tool call has run.",
            "link": "View the audit log",
        },
    },
    "audit": {
        "zh": {
            "subject": "Saturn：{tool} 已寫入稽核",
            "title": "已寫入稽核",
            "body": "{who}呼叫 {tool} 已寫入稽核紀錄。",
            "empty": "有一個工具呼叫已寫入稽核紀錄。",
            "link": "查看稽核紀錄",
        },
        "en": {
            "subject": "Saturn: {tool} was audited",
            "title": "Audit record",
            "body": "{who} called {tool}, and Saturn wrote it to the audit log.",
            "empty": "A tool call was written to the audit log.",
            "link": "View the audit log",
        },
    },
}


def _notice_copy(kind: str, language: str, tool: str, agent: str) -> tuple[str, str, str, str]:
    copy = _NOTICE[kind][language]
    name = tool or ("工具呼叫" if language == "zh" else "a tool")
    if agent and tool:
        body = copy["body"].format(who=agent, tool=name)
    else:
        body = copy["empty"] if not tool else copy["body"].format(who=agent or ("有一個呼叫" if language == "zh" else "Something"), tool=name)
    return copy["subject"].format(tool=name), copy["title"], body, copy["link"]


def _notice_message(kind: str, tool: str, agent: str, link: str, locale: str) -> tuple[str, str, str]:
    subject, title, body, link_label = _notice_copy(kind, _copy_language(locale), tool, agent)
    text = f"{title}\n\n{body}\n\n{link_label}: {link}\n\nSaturn\n"
    button = (
        f'<p style="margin:24px 0 0;"><a href="{escape(link)}" style="display:inline-block;padding:10px 16px;'
        f'background:#111110;color:#ffffff;border-radius:8px;font-size:13px;text-decoration:none;">{escape(link_label)}</a></p>'
    )
    inner = f"""<h1 style="margin:28px 0 0;font-size:22px;line-height:1.3;font-weight:600;letter-spacing:-0.02em;">{escape(title)}</h1>
              <p style="margin:22px 0 0;font-size:14px;line-height:1.6;">{escape(body)}</p>
              {button}"""
    return subject, text, _card(normalize_locale(locale), title, inner)


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

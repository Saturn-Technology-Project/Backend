from __future__ import annotations

import base64
import re

from fastapi import HTTPException, status

_PHOTO = re.compile(r"^data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)$")
_MAX_BYTES = 512 * 1024


def clean_photo(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    match = _PHOTO.fullmatch(text)
    if match is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Photo must be a PNG, JPEG, or WebP image")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Photo must be a PNG, JPEG, or WebP image") from exc
    if not raw or len(raw) > _MAX_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Photo is too large")
    return text

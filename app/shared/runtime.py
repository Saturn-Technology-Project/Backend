"""Where this process runs.

Some conveniences (returning one-time tokens in API responses, letting an admin
choose a member's password) exist only so local development and the test suite
can work without email. They must never be on in production.
"""

from __future__ import annotations

import os

from app.core.config import get_settings


def local_dev_mode() -> bool:
    """True only for local development and tests.

    Production runs on Cloud Run, which always sets K_SERVICE, and does not run
    the in-process runtime. Both must say "local" before we relax anything.
    """
    if os.environ.get("K_SERVICE", "").strip():
        return False
    return bool(get_settings().in_process_runtime)


def email_configured() -> bool:
    settings = get_settings()
    return bool(settings.resend_api_key and settings.resend_from)

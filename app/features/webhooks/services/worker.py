from __future__ import annotations

import asyncio
import logging

from app.db.database import SessionLocal
from app.features.execution.approvals.services.expiration import drop_expired_content, expire_due
from app.features.webhooks.services.webhook_service import deliver_due

logger = logging.getLogger(__name__)

POLL_SECONDS = 5


async def run_webhook_worker() -> None:
    while True:
        try:
            async with SessionLocal() as session:
                await expire_due(session)
                await drop_expired_content(session)
                await deliver_due(session)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Webhook worker failed")
        await asyncio.sleep(POLL_SECONDS)

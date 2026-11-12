"""Run blocking work off the event loop.

The API runs one event loop per instance. A password hash takes tens of milliseconds of CPU
and a DNS lookup can take seconds; done inline, every other request on the instance waits.
"""

from __future__ import annotations

import asyncio

in_thread = asyncio.to_thread

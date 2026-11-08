"""Count a guess before checking it, so concurrent guesses cannot share one attempt."""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value


async def reserve_attempt(session: AsyncSession, row: Any, column: str, limit: int) -> int | None:
    """Add one to row.<column> if it is below limit, atomically. Returns the new count, or None.

    A read-modify-write lets N parallel requests all read the same count and each make a guess,
    so a cap of 5 allowed 5 guesses per wave. A conditional UPDATE serialises them on the row:
    once the count reaches limit no further request can reserve a guess.
    """
    await session.flush()
    model = type(row)
    counter = getattr(model, column)
    result = await session.execute(
        update(model)
        .where(model.id == row.id, counter < limit)
        .values({column: counter + 1})
        .returning(counter)
        .execution_options(synchronize_session=False)
    )
    value = result.scalar_one_or_none()
    if value is None:
        set_committed_value(row, column, limit)
        return None
    set_committed_value(row, column, int(value))
    return int(value)

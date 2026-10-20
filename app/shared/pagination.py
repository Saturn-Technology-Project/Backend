"""Optional limit/offset paging for list endpoints.

The response shape stays a plain list. Without query parameters a list returns at
most DEFAULT_LIMIT rows, so no single request can make the server load and
serialize an unbounded table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query

DEFAULT_LIMIT = 200
MAX_LIMIT = 500
MAX_OFFSET = 1_000_000


@dataclass(frozen=True)
class PageParams:
    limit: int = DEFAULT_LIMIT
    offset: int = 0

    def apply(self, statement):
        return statement.limit(self.limit).offset(self.offset)

    def slice(self, items: list) -> list:
        return items[self.offset : self.offset + self.limit]


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


Page = Annotated[PageParams, Depends(page_params)]

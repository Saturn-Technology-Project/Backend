"""Seal tool results that older versions stored readable. This does not run on startup.

An approved call keeps its upstream result so a repeated call can be answered without
running it again. Those results used to be stored as they were. This seals each one with
its organization's content key, the same way new results are stored.

    python -m app.cli.seal_results            # dry run: counts only
    python -m app.cli.seal_results --apply

Nothing is printed but counts. The process needs the same KMS access as the API.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.execution.approvals.services.approval_arguments import is_server_sealed, seal_result
from app.models import Action, Run

BATCH = 200


async def seal_plain_results(session: AsyncSession, apply: bool) -> int:
    """Seal every readable gateway result. Returns how many rows were, or would be, changed."""
    changed = 0
    last = ""
    while True:
        rows = (
            await session.execute(
                select(Action, Run.organization_id)
                .join(Run, Run.id == Action.run_id)
                .where(Action.id > last, Action.result.is_not(None), Action.policy_reason == "saturn_mcp")
                .order_by(Action.id)
                .limit(BATCH)
            )
        ).all()
        if not rows:
            return changed
        for action, organization_id in rows:
            last = action.id
            if not isinstance(action.result, dict) or is_server_sealed(action.result):
                continue
            changed += 1
            if apply:
                action.result = seal_result(organization_id, action.result)
        if apply:
            await session.commit()


async def _run(apply: bool) -> int:
    from app.db.database import SessionLocal

    async with SessionLocal() as session:
        changed = await seal_plain_results(session, apply)
    print(f"{'sealed' if apply else 'would seal'} {changed} results")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in ([], ["--apply"]):
        print("usage: python -m app.cli.seal_results [--apply]", file=sys.stderr)
        return 2
    return asyncio.run(_run(bool(args)))


if __name__ == "__main__":
    raise SystemExit(main())

"""List or delete leftover pq1 ciphertext. This does not run on startup.

    python -m app.cli.pq1 list
    python -m app.cli.pq1 delete
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import SessionLocal
from app.features.credentials.vault.models.credential import OrgCredential
from app.features.credentials.vault.models.secret_vault import SecretVault
from app.features.mcp.oauth.models.mcp_oauth import McpOAuthPending

_PREFIX = "pq1."


async def pq1_locations(session: AsyncSession) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    vaults = await session.scalars(select(SecretVault).where(SecretVault.ciphertext.startswith(_PREFIX)))
    found.extend(("secret_vault", row.id) for row in vaults.all())
    credentials = await session.scalars(
        select(OrgCredential).where(OrgCredential.secret_ciphertext.startswith(_PREFIX))
    )
    # Credential ids are unique per organization, so the location names both.
    found.extend(("org_credentials", f"{row.organization_id}/{row.id}") for row in credentials.all())
    pending = await session.scalars(select(McpOAuthPending))
    for row in pending.all():
        if row.verifier_ciphertext.startswith(_PREFIX) or row.client_secret_ciphertext.startswith(_PREFIX):
            found.append(("mcp_oauth_pending", row.id))
    return found


async def delete_pq1(session: AsyncSession) -> list[tuple[str, str]]:
    found = await pq1_locations(session)
    for table, row_id in found:
        if table == "secret_vault":
            row = await session.get(SecretVault, row_id)
            if row is not None:
                await session.delete(row)
        elif table == "org_credentials":
            organization_id, _, credential_id = row_id.partition("/")
            row = await session.get(OrgCredential, {"organization_id": organization_id, "id": credential_id})
            if row is not None:
                row.secret_ciphertext = ""
        elif table == "mcp_oauth_pending":
            row = await session.get(McpOAuthPending, row_id)
            if row is not None:
                await session.delete(row)
    await session.flush()
    return found


async def _run(action: str) -> int:
    async with SessionLocal() as session:
        if action == "list":
            found = await pq1_locations(session)
        else:
            found = await delete_pq1(session)
            await session.commit()
    for table, row_id in found:
        print(f"{table} {row_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in (["list"], ["delete"]):
        print("usage: python -m app.cli.pq1 list|delete", file=sys.stderr)
        return 2
    return asyncio.run(_run(args[0]))


if __name__ == "__main__":
    raise SystemExit(main())

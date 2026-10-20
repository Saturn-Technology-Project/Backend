"""Member key approval, stretched recovery backups and per-organization credential ids.

- member_payload_keys.approval / approved_by / approved_at: a newly registered member key is
  pending until a team.write admin approves it, and only approved keys receive DEK wraps.
  Keys that already exist are kept approved so current members do not lose access; admins can
  revoke them.
- org_key_backups.iterations: PBKDF2 iteration count for v2 recovery backups (0 for legacy HKDF).
- org_credentials primary key becomes (organization_id, id), so a client-chosen id no longer
  reveals or blocks another organization's credential.

Revision ID: 055_payload_keys_hardening
Revises: 053_auth_hardening
Create Date: 2026-10-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "055_payload_keys_hardening"
down_revision: Union[str, Sequence[str], None] = "053_auth_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    member = _columns("member_payload_keys")
    if "approval" not in member:
        op.add_column(
            "member_payload_keys",
            sa.Column("approval", sa.String(length=16), nullable=False, server_default="approved"),
        )
    if "approved_by" not in member:
        op.add_column(
            "member_payload_keys",
            sa.Column("approved_by", sa.String(length=64), nullable=False, server_default=""),
        )
    if "approved_at" not in member:
        op.add_column("member_payload_keys", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    if "iterations" not in _columns("org_key_backups"):
        op.add_column(
            "org_key_backups",
            sa.Column("iterations", sa.Integer(), nullable=False, server_default="0"),
        )
    pk = inspect(op.get_bind()).get_pk_constraint("org_credentials")
    if list(pk.get("constrained_columns") or []) == ["id"]:
        with op.batch_alter_table("org_credentials") as batch:
            batch.drop_constraint(pk.get("name") or "org_credentials_pkey", type_="primary")
            batch.create_primary_key("org_credentials_pkey", ["organization_id", "id"])


def downgrade() -> None:
    pk = inspect(op.get_bind()).get_pk_constraint("org_credentials")
    if list(pk.get("constrained_columns") or []) != ["id"]:
        shared = op.get_bind().execute(
            sa.text("SELECT id FROM org_credentials GROUP BY id HAVING COUNT(*) > 1 LIMIT 5")
        ).scalars().all()
        if shared:
            raise RuntimeError(
                "Cannot downgrade: these credential ids are now used by more than one organization: "
                + ", ".join(shared)
                + ". Rename or delete the extra rows first."
            )
        with op.batch_alter_table("org_credentials") as batch:
            batch.drop_constraint(pk.get("name") or "org_credentials_pkey", type_="primary")
            batch.create_primary_key("org_credentials_pkey", ["id"])
    op.drop_column("org_key_backups", "iterations")
    op.drop_column("member_payload_keys", "approved_at")
    op.drop_column("member_payload_keys", "approved_by")
    op.drop_column("member_payload_keys", "approval")

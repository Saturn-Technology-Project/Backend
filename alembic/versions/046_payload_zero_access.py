"""Organization payload keys and zero-access envelopes.

Revision ID: 046_payload_zero_access
Revises: 045_sealed_content
Create Date: 2026-10-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "046_payload_zero_access"
down_revision: Union[str, Sequence[str], None] = "045_sealed_content"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if "org_payload_keys" not in tables:
        op.create_table(
            "org_payload_keys",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("organization_id", sa.String(length=64), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("public_key", sa.String(length=64), nullable=False),
            sa.Column("algorithm", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("owner_user_id", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("organization_id", "version", name="uq_org_payload_key_version"),
        )
    if "org_key_backups" not in tables:
        op.create_table(
            "org_key_backups",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("organization_id", sa.String(length=64), nullable=False),
            sa.Column("key_version", sa.Integer(), nullable=False),
            sa.Column("format_version", sa.Integer(), nullable=False),
            sa.Column("kdf", sa.String(length=64), nullable=False),
            sa.Column("salt", sa.String(length=128), nullable=False),
            sa.Column("nonce", sa.String(length=64), nullable=False),
            sa.Column("ciphertext", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "member_payload_keys" not in tables:
        op.create_table(
            "member_payload_keys",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("organization_id", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("public_key", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "member_key_envelopes" not in tables:
        op.create_table(
            "member_key_envelopes",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("organization_id", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("member_key_id", sa.String(length=64), nullable=False),
            sa.Column("org_key_version", sa.Integer(), nullable=False),
            sa.Column("wrapped_private_key", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "payload_rotations" not in tables:
        op.create_table(
            "payload_rotations",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("organization_id", sa.String(length=64), nullable=False),
            sa.Column("from_version", sa.Integer(), nullable=False),
            sa.Column("to_version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("migrated", sa.Integer(), nullable=False),
            sa.Column("error", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    for name in (
        "payload_rotations",
        "member_key_envelopes",
        "member_payload_keys",
        "org_key_backups",
        "org_payload_keys",
    ):
        op.drop_table(name)

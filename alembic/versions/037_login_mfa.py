"""Email confirmation and sign-in second factors

Revision ID: 037_login_mfa
Revises: 036_drop_org_addons
Create Date: 2026-10-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "037_login_mfa"
down_revision: Union[str, Sequence[str], None] = "036_drop_org_addons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {item["name"] for item in inspect(bind).get_columns(table)}


def _has_table(table: str) -> bool:
    return inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if not _has_column("users", "email_mfa"):
        op.add_column(
            "users",
            sa.Column("email_mfa", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if not _has_table("email_codes"):
        op.create_table(
            "email_codes",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("purpose", sa.String(length=16), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=True),
            sa.Column("password_hash", sa.String(length=255), nullable=True),
            sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("code_hash", sa.String(length=255), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_email_codes_email", "email_codes", ["email"])
    if not _has_table("login_challenges"):
        op.create_table(
            "login_challenges",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("code_hash", sa.String(length=255), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_login_challenges_user_id", "login_challenges", ["user_id"])
        op.create_index("ix_login_challenges_token_hash", "login_challenges", ["token_hash"], unique=True)
    if not _has_table("passkeys"):
        op.create_table(
            "passkeys",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("credential_id", sa.String(length=512), nullable=False),
            sa.Column("public_key", sa.Text(), nullable=False),
            sa.Column("sign_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_passkeys_user_id", "passkeys", ["user_id"])
        op.create_index("ix_passkeys_credential_id", "passkeys", ["credential_id"], unique=True)
    if not _has_table("webauthn_challenges"):
        op.create_table(
            "webauthn_challenges",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("purpose", sa.String(length=16), nullable=False),
            sa.Column("challenge", sa.String(length=128), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_webauthn_challenges_user_id", "webauthn_challenges", ["user_id"])


def downgrade() -> None:
    if _has_table("webauthn_challenges"):
        op.drop_table("webauthn_challenges")
    if _has_table("passkeys"):
        op.drop_table("passkeys")
    if _has_table("login_challenges"):
        op.drop_table("login_challenges")
    if _has_table("email_codes"):
        op.drop_table("email_codes")
    if _has_column("users", "email_mfa"):
        op.drop_column("users", "email_mfa")

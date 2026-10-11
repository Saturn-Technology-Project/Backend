"""Webhook outbox and approval expiration windows

Revision ID: 044_webhooks_expiry
Revises: 043_notice_deliveries
Create Date: 2026-10-12

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "044_webhooks_expiry"
down_revision: Union[str, Sequence[str], None] = "043_notice_deliveries"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "approvals" in tables:
        columns = _columns(bind, "approvals")
        if "resubmit_expires_at" not in columns:
            op.add_column("approvals", sa.Column("resubmit_expires_at", sa.DateTime(timezone=True), nullable=True))
        if "expired_phase" not in columns:
            op.add_column("approvals", sa.Column("expired_phase", sa.String(length=32), nullable=True))
    if "approval_policies" in tables:
        columns = _columns(bind, "approval_policies")
        if "resubmit_minutes" not in columns:
            op.add_column("approval_policies", sa.Column("resubmit_minutes", sa.Integer(), nullable=True))
    if "approval_windows" not in tables:
        op.create_table(
            "approval_windows",
            sa.Column("organization_id", sa.String(length=64), nullable=False),
            sa.Column("pending_ttl_minutes", sa.Integer(), nullable=False),
            sa.Column("resubmit_ttl_minutes", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
            sa.PrimaryKeyConstraint("organization_id"),
        )
    if "webhook_endpoints" not in tables:
        op.create_table(
            "webhook_endpoints",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("organization_id", sa.String(length=64), nullable=False),
            sa.Column("url", sa.String(length=2000), nullable=False),
            sa.Column("events", sa.JSON(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("secret_id", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_webhook_endpoints_organization_id", "webhook_endpoints", ["organization_id"])
    if "webhook_deliveries" not in tables:
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("organization_id", sa.String(length=64), nullable=False),
            sa.Column("endpoint_id", sa.String(length=64), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("event_key", sa.String(length=200), nullable=False),
            sa.Column("event_id", sa.String(length=64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("attempt_count", sa.Integer(), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_status_code", sa.Integer(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["endpoint_id"], ["webhook_endpoints.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("endpoint_id", "event_key", name="uq_webhook_delivery_event"),
            sa.UniqueConstraint("event_id"),
        )
        op.create_index("ix_webhook_deliveries_organization_id", "webhook_deliveries", ["organization_id"])
        op.create_index("ix_webhook_deliveries_endpoint_id", "webhook_deliveries", ["endpoint_id"])
        op.create_index("ix_webhook_deliveries_status", "webhook_deliveries", ["status"])
    if "webhook_attempts" not in tables:
        op.create_table(
            "webhook_attempts",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("delivery_id", sa.String(length=64), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=True),
            sa.Column("error", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["delivery_id"], ["webhook_deliveries.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_webhook_attempts_delivery_id", "webhook_attempts", ["delivery_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "webhook_attempts" in tables:
        op.drop_table("webhook_attempts")
    if "webhook_deliveries" in tables:
        op.drop_table("webhook_deliveries")
    if "webhook_endpoints" in tables:
        op.drop_table("webhook_endpoints")
    if "approval_windows" in tables:
        op.drop_table("approval_windows")
    if "approval_policies" in tables and "resubmit_minutes" in _columns(bind, "approval_policies"):
        op.drop_column("approval_policies", "resubmit_minutes")
    if "approvals" in tables:
        columns = _columns(bind, "approvals")
        if "expired_phase" in columns:
            op.drop_column("approvals", "expired_phase")
        if "resubmit_expires_at" in columns:
            op.drop_column("approvals", "resubmit_expires_at")

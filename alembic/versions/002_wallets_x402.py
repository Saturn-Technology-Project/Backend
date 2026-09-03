"""agent wallets and x402 payments

Revision ID: 002_wallets_x402
Revises: 001_initial
Create Date: 2026-09-02

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002_wallets_x402"
down_revision: Union[str, Sequence[str], None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tools", sa.Column("x402", sa.JSON(), nullable=True))
    op.create_table(
        "wallets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("organization_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("address", sa.String(length=66), nullable=False),
        sa.Column("network", sa.String(length=64), nullable=False),
        sa.Column("asset", sa.String(length=16), nullable=False),
        sa.Column("balance", sa.Numeric(18, 6), nullable=False),
        sa.Column("daily_spend_limit", sa.Numeric(18, 6), nullable=False),
        sa.Column("per_tx_limit", sa.Numeric(18, 6), nullable=False),
        sa.Column("spent_today", sa.Numeric(18, 6), nullable=False),
        sa.Column("spend_reset_on", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", name="uq_wallets_agent_id"),
    )
    op.create_index("ix_wallets_organization_id", "wallets", ["organization_id"])
    op.create_index("ix_wallets_agent_id", "wallets", ["agent_id"])
    op.create_table(
        "payments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("wallet_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("action_id", sa.String(length=64), nullable=True),
        sa.Column("tool", sa.String(length=255), nullable=True),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("asset", sa.String(length=16), nullable=False),
        sa.Column("network", sa.String(length=64), nullable=False),
        sa.Column("pay_to", sa.String(length=66), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("tx_hash", sa.String(length=66), nullable=True),
        sa.Column("requirements", sa.JSON(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("facilitator", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["action_id"], ["actions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("action_id"),
    )
    op.create_index("ix_payments_wallet_id", "payments", ["wallet_id"])
    op.create_index("ix_payments_run_id", "payments", ["run_id"])
    op.create_index("ix_payments_action_id", "payments", ["action_id"])
    op.create_index("ix_payments_status", "payments", ["status"])


def downgrade() -> None:
    op.drop_table("payments")
    op.drop_table("wallets")
    op.drop_column("tools", "x402")

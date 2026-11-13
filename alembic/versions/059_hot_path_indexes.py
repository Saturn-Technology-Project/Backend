"""Indexes for the queries that run on every request and every worker tick.

- audit_logs (organization_id, created_at, id): the activity list reads one organization
  newest first. Only single-column indexes existed, so it sorted the organization's whole log.
- audit_logs (action_id): looked up when an agent or run is removed.
- approvals (expires_at) and (resubmit_expires_at), partial: the expiry worker asks every few
  seconds which open approvals are past a deadline.
- oauth_clients (name): client registration looks for an existing client with the same name.
- The payload key tables: their models declare these indexes, but the migration that created
  the tables did not, so a database built from migrations never had them.

Each index is skipped when it already exists. These are plain CREATE INDEX statements inside
the migration transaction, which blocks writes to the table while it builds. On a large
audit_logs, create that one by hand first, and this migration then leaves it alone:

    CREATE INDEX CONCURRENTLY ix_audit_logs_org_created
        ON audit_logs (organization_id, created_at, id);

Revision ID: 059_hot_path_indexes
Revises: 058_mcp_oauth_browser
Create Date: 2026-11-12

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "059_hot_path_indexes"
down_revision: Union[str, Sequence[str], None] = "058_mcp_oauth_browser"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index name, table, columns, partial condition)
_INDEXES: list[tuple[str, str, list[str], str | None]] = [
    ("ix_audit_logs_org_created", "audit_logs", ["organization_id", "created_at", "id"], None),
    ("ix_audit_logs_action_id", "audit_logs", ["action_id"], None),
    ("ix_approvals_pending_expiry", "approvals", ["expires_at"], "status = 'pending'"),
    (
        "ix_approvals_resubmit_expiry",
        "approvals",
        ["resubmit_expires_at"],
        "status = 'approved' AND execution_state = 'none'",
    ),
    ("ix_oauth_clients_name", "oauth_clients", ["name"], None),
    ("ix_org_payload_keys_organization_id", "org_payload_keys", ["organization_id"], None),
    ("ix_org_key_backups_organization_id", "org_key_backups", ["organization_id"], None),
    ("ix_member_key_backups_organization_id", "member_key_backups", ["organization_id"], None),
    ("ix_member_key_backups_user_id", "member_key_backups", ["user_id"], None),
    ("ix_member_key_backups_member_key_id", "member_key_backups", ["member_key_id"], None),
    ("ix_member_payload_keys_organization_id", "member_payload_keys", ["organization_id"], None),
    ("ix_member_payload_keys_user_id", "member_payload_keys", ["user_id"], None),
    ("ix_member_key_envelopes_organization_id", "member_key_envelopes", ["organization_id"], None),
    ("ix_member_key_envelopes_user_id", "member_key_envelopes", ["user_id"], None),
    ("ix_payload_rotations_organization_id", "payload_rotations", ["organization_id"], None),
]


def _existing(table: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    for name, table, columns, where in _INDEXES:
        if table not in tables or name in _existing(table):
            continue
        condition = sa.text(where) if where else None
        op.create_index(name, table, columns, postgresql_where=condition, sqlite_where=condition)


def downgrade() -> None:
    for name, table, _columns, _where in reversed(_INDEXES):
        if name in _existing(table):
            op.drop_index(name, table_name=table)

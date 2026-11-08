"""Re-seal every KMS-rooted value under a new KMS MAC key. This does not run on startup.

Moving Saturn to a new Google Cloud project means a new Cloud KMS key, and a KMS key
cannot be exported. Everything the old key protects is opened with the old key and
sealed again with the new one, in one transaction:

- pq2 credentials (vault entries, org credentials, MCP OAuth secrets, TOTP secrets),
- s2 sealed content (audit payloads, tool arguments),
- keyed approval digests, recomputed from the arguments they bind.

    python -m app.cli.reseal --old-key OLD_VERSION --new-key NEW_VERSION           # dry run
    python -m app.cli.reseal --old-key OLD_VERSION --new-key NEW_VERSION --apply

The process needs MAC sign permission on both key versions. Nothing is printed but table
names, row keys and counts.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import re
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM1024PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import MetaData, Table, select, update

from app.features.credentials.vault.services.pq_crypto import _INFO_AES, _PREFIX as PQ_PREFIX, _hkdf

S2_PREFIX = "s2."
_DIGEST_INFO = b"saturn-approval-digest-v1\n"
_SERVER_SEALED = "server-sealed-v1"


class Keys:
    """The keys pq_crypto derives, from one MAC function (a KMS key version, or a local root in tests)."""

    def __init__(self, mac: Callable[[bytes], bytes]) -> None:
        self._mac = mac
        self._private: dict[tuple[str, str], MLKEM1024PrivateKey] = {}
        self._content: dict[str, bytes] = {}

    def private(self, organization_id: str, user_id: str) -> MLKEM1024PrivateKey:
        found = self._private.get((organization_id, user_id))
        if found is None:
            label = f"saturn-mlkem-v1\n{organization_id}\n{user_id}\n".encode()
            found = MLKEM1024PrivateKey.from_seed_bytes(self._mac(label + b"0") + self._mac(label + b"1"))
            self._private[(organization_id, user_id)] = found
        return found

    def content_key(self, organization_id: str) -> bytes:
        found = self._content.get(organization_id)
        if found is None:
            found = self._mac(f"saturn-org-content-v2\n{organization_id}".encode())
            self._content[organization_id] = found
        return found

    def seal(self, plain: str, organization_id: str, user_id: str) -> str:
        shared, kem_ciphertext = self.private(organization_id, user_id).public_key().encapsulate()
        nonce = os.urandom(12)
        encrypted = AESGCM(_hkdf(shared, _INFO_AES, 32)).encrypt(nonce, plain.encode(), None)
        return PQ_PREFIX + base64.b64encode(kem_ciphertext + nonce + encrypted).decode()

    def open(self, token: str, organization_id: str, user_id: str) -> str | None:
        """The plaintext, or None when this tenant key does not open the token."""
        try:
            raw = base64.b64decode(token[len(PQ_PREFIX) :])
            kem_ciphertext, rest = raw[:1568], raw[1568:]
            shared = self.private(organization_id, user_id).decapsulate(kem_ciphertext)
            return AESGCM(_hkdf(shared, _INFO_AES, 32)).decrypt(rest[:12], rest[12:], None).decode()
        except Exception:
            return None

    def seal_content(self, body: bytes, organization_id: str) -> str:
        nonce = os.urandom(12)
        return S2_PREFIX + (nonce + AESGCM(self.content_key(organization_id)).encrypt(nonce, body, None)).hex()

    def open_content(self, token: str, organization_id: str) -> bytes | None:
        try:
            raw = bytes.fromhex(token[len(S2_PREFIX) :])
            return AESGCM(self.content_key(organization_id)).decrypt(raw[:12], raw[12:], None)
        except Exception:
            return None

    def digest(self, organization_id: str, arguments: Any) -> str:
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=_DIGEST_INFO + organization_id.encode()
        ).derive(self.content_key(organization_id))
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def kms_mac(name: str) -> Callable[[bytes], bytes]:
    from google.cloud import kms

    client = kms.KeyManagementServiceClient()

    def mac(data: bytes) -> bytes:
        value = bytes(client.mac_sign(request={"name": name, "data": data}).mac)
        if len(value) != 32:
            raise RuntimeError("KMS MAC was not 32 bytes")
        return value

    return mac


@dataclass
class Report:
    resealed: dict[str, int] = field(default_factory=dict)
    digests: int = 0
    unopened: list[str] = field(default_factory=list)
    digests_left: list[str] = field(default_factory=list)
    # Prefix-shaped text where Saturn never writes a sealed value. Reported, never blocking:
    # a tenant can type "pq2.x" into a name or a prompt.
    ignored: list[str] = field(default_factory=list)

    def count(self, table: str) -> None:
        self.resealed[table] = self.resealed.get(table, 0) + 1


@dataclass
class Tenants:
    """Every organization and its users: the candidates for a value whose row does not name its tenant."""

    users: dict[str, str]  # user id -> organization id
    organizations: list[str]

    def organizations_for(self, row: dict[str, Any]) -> list[str]:
        named = [row.get("organization_id"), self.users.get(str(row.get("user_id") or ""))]
        first = [str(value) for value in named if value]
        return list(dict.fromkeys(first + self.organizations))

    def contexts_for(self, table: str, row: dict[str, Any]) -> list[tuple[str, str]]:
        """(organization, user) pairs to try, the ones the row names first."""
        named_users = [row.get("user_id"), row.get("owner_user_id")]
        if table == "users":
            named_users.insert(0, row.get("id"))
        named = [str(value) for value in named_users if value]
        contexts: list[tuple[str, str]] = []
        for organization_id in self.organizations_for(row):
            members = [user for user, org in self.users.items() if org == organization_id]
            for user_id in [*named, "", *members]:
                contexts.append((organization_id, user_id))
        return list(dict.fromkeys(contexts))


# Where Saturn writes sealed values: vault and credential *ciphertext columns, users.totp_secret,
# and the "sealed" key of JSON arguments and payloads.
_SEALED_FIELDS = ("totp_secret", "sealed")


def sealed_location(location: str) -> bool:
    last = re.split(r"[ .]", location)[-1]
    last = re.sub(r"\[\d+\]$", "", last)
    return last.endswith("ciphertext") or last in _SEALED_FIELDS


class Resealer:
    def __init__(self, old: Keys, new: Keys, tenants: Tenants, report: Report) -> None:
        self.old, self.new, self.tenants, self.report = old, new, tenants, report

    def _unopened(self, location: str) -> None:
        if sealed_location(location):
            self.report.unopened.append(location)
        else:
            self.report.ignored.append(location)

    def value(self, table: str, location: str, row: dict[str, Any], value: Any) -> tuple[Any, bool]:
        """The value with every sealed string inside it re-sealed, and whether anything changed."""
        if isinstance(value, str):
            if value.startswith(PQ_PREFIX):
                for organization_id, user_id in self.tenants.contexts_for(table, row):
                    plain = self.old.open(value, organization_id, user_id)
                    if plain is not None:
                        return self.new.seal(plain, organization_id, user_id), True
                self._unopened(location)
            elif value.startswith(S2_PREFIX):
                for organization_id in self.tenants.organizations_for(row):
                    body = self.old.open_content(value, organization_id)
                    if body is not None:
                        return self.new.seal_content(body, organization_id), True
                self._unopened(location)
            return value, False
        if isinstance(value, dict):
            changed = False
            result = {}
            for key, item in value.items():
                result[key], item_changed = self.value(table, f"{location}.{key}", row, item)
                changed = changed or item_changed
            return result, changed
        if isinstance(value, list):
            changed = False
            result_list = []
            for index, item in enumerate(value):
                new_item, item_changed = self.value(table, f"{location}[{index}]", row, item)
                result_list.append(new_item)
                changed = changed or item_changed
            return result_list, changed
        return value, False


def _plain_arguments(keys: Keys, organization_id: str, stored: Any) -> Any:
    """The arguments an approval's digest was computed over, opened with the given keys if sealed."""
    if isinstance(stored, dict) and isinstance(stored.get("sealed"), str) and set(stored) <= {"sealed", "encryption"}:
        if stored.get("encryption", _SERVER_SEALED) != _SERVER_SEALED:
            return None
        body = keys.open_content(stored["sealed"], organization_id)
        return json.loads(body) if body is not None else None
    return stored


async def reseal(connection: Any, old: Keys, new: Keys, apply: bool) -> Report:
    """Re-seal the whole database through an AsyncConnection. Writes only when apply is True."""
    metadata = MetaData()
    await connection.run_sync(metadata.reflect)
    tables: dict[str, Table] = dict(metadata.tables)
    users = {str(r.id): str(r.organization_id) for r in await connection.execute(select(tables["users"].c.id, tables["users"].c.organization_id))}
    organizations = [str(r.id) for r in await connection.execute(select(tables["organizations"].c.id))]
    report = Report()
    resealer = Resealer(old, new, Tenants(users, organizations), report)

    # Digests first: they are recomputed from arguments opened with the old content key.
    digest_updates: list[tuple[Any, str]] = []
    if {"approvals", "actions", "runs"} <= tables.keys():
        approvals, actions, runs = tables["approvals"], tables["actions"], tables["runs"]
        query = (
            select(approvals.c.id, approvals.c.parameter_digest, actions.c.arguments, runs.c.organization_id)
            .join(actions, actions.c.id == approvals.c.action_id)
            .join(runs, runs.c.id == approvals.c.run_id)
            .where(approvals.c.parameter_digest.is_not(None))
        )
        for row in await connection.execute(query):
            organization_id = str(row.organization_id)
            arguments = _plain_arguments(old, organization_id, row.arguments)
            if arguments is not None and hmac.compare_digest(row.parameter_digest, old.digest(organization_id, arguments)):
                digest_updates.append((row.id, new.digest(organization_id, arguments)))
            else:
                report.digests_left.append(f"approvals {row.id}")

    for name, table in tables.items():
        if name == "alembic_version":
            continue
        columns = list(table.columns)
        keys = list(table.primary_key.columns)
        if not keys:
            continue
        for row in (await connection.execute(select(table))).mappings().all():
            data = dict(row)
            changes: dict[str, Any] = {}
            for column in columns:
                current = data[column.name]
                if not isinstance(current, (str, dict, list)):
                    continue
                row_key = "/".join(str(data[key.name]) for key in keys)
                new_value, changed = resealer.value(name, f"{name} {row_key} {column.name}", data, current)
                if changed:
                    changes[column.name] = new_value
            if changes:
                report.count(name)
                if apply:
                    condition = [key == data[key.name] for key in keys]
                    await connection.execute(update(table).where(*condition).values(**changes))

    report.digests = len(digest_updates)
    if apply:
        approvals = tables["approvals"]
        for approval_id, digest in digest_updates:
            await connection.execute(update(approvals).where(approvals.c.id == approval_id).values(parameter_digest=digest))
    return report


async def _run(old_key: str, new_key: str, apply: bool, allow_unopened: bool) -> int:
    from app.db.database import engine

    old, new = Keys(kms_mac(old_key)), Keys(kms_mac(new_key))
    async with engine.connect() as connection:
        transaction = await connection.begin()
        report = await reseal(connection, old, new, apply)
        for table, count in sorted(report.resealed.items()):
            print(f"resealed {table}: {count} rows")
        print(f"approval digests recomputed: {report.digests}")
        for location in report.digests_left:
            print(f"digest left as is: {location}")
        for location in report.unopened:
            print(f"could not open: {location}")
        for location in report.ignored:
            print(f"left as text (not a sealed field): {location}")
        if report.unopened and not allow_unopened:
            await transaction.rollback()
            print("rolled back: some values did not open with the old key", file=sys.stderr)
            return 1
        if apply:
            await transaction.commit()
            print("committed")
        else:
            await transaction.rollback()
            print("dry run: nothing written")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli.reseal", description=__doc__.splitlines()[0])
    parser.add_argument("--old-key", required=True, help="full cryptoKeyVersions name of the old MAC key")
    parser.add_argument("--new-key", required=True, help="full cryptoKeyVersions name of the new MAC key")
    parser.add_argument("--apply", action="store_true", help="write the re-sealed values (default: dry run)")
    parser.add_argument("--allow-unopened", action="store_true", help="commit even if some values did not open")
    args = parser.parse_args(argv)
    if args.old_key == args.new_key:
        parser.error("the old and new keys are the same")
    return asyncio.run(_run(args.old_key, args.new_key, args.apply, args.allow_unopened))


if __name__ == "__main__":
    raise SystemExit(main())

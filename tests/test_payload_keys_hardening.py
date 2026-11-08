"""Regression tests for the payload-key, credential and activity hardening (H-05, H-08, M-06,
M-07, M-10, L-27, L-29, I-07)."""

import os
from types import SimpleNamespace

import pytest

from app.features.execution.activity.services.zero_access import (
    content_hash,
    decrypt_payload,
    dek_proof,
    encrypt_payload,
    generate_mlkem,
    legacy_recovery_backup,
    open_recovery_backup,
    recovery_backup,
    unwrap_secret,
    wrap_secret,
)
from tests.oauth_access import oauth_access
from tests.policy_helpers import activate_created
from tests.test_quorum import _auth, _people, _request

BAD_KEY = "ff" * 1568


async def _db(client):
    from app.db.database import get_session
    from app.main import app

    generator = app.dependency_overrides[get_session]()
    session = await anext(generator)
    return generator, session


async def _org_id(client) -> str:
    return (await client.get("/org")).json()["organization"]["id"]


async def _org_key(client):
    private, public = generate_mlkem()
    created = await client.post(
        "/org/payload-keys",
        json={"public_key": public.hex(), "backup": recovery_backup(os.urandom(32), "org", 1, private)},
    )
    assert created.status_code == 200, created.text
    return private, public


async def _gateway(client, monkeypatch, name="Zero Agent"):
    async def listed(endpoint, authorization=""):
        return [{"name": "repo.read", "description": "Read", "inputSchema": {"type": "object"}}]

    async def called(endpoint, tool, arguments, authorization=""):
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.saturn.services.saturn_mcp.call_remote_tool", called)
    connected = await client.post(
        "/mcp-servers",
        json={"name": "GitHub", "endpoint": "https://api.githubcopilot.com/mcp", "authorization": "token-1"},
    )
    tool_id = connected.json()["catalog"][0]["id"]
    tool_set = await client.post("/tool-sets", json={"name": "Reads", "items": [{"tool_id": tool_id, "approval": False}]})
    access = await oauth_access(client, name)
    await client.post("/mcp", headers={"Authorization": f"Bearer {access}"}, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    agent_id = next(item["id"] for item in (await client.get("/agents")).json() if item["name"] == name)
    await client.patch(f"/agents/{agent_id}", json={"tool_set_id": tool_set.json()["id"]})

    async def call(arguments):
        sent = await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {access}", "X-Saturn-Agent-Id": agent_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "repo.read", "arguments": arguments}},
        )
        assert sent.status_code == 200, sent.text
        rows = (await client.get("/activity", params={"tool": "repo.read"})).json()
        return next(item for item in rows if item["event"] == "tool_requested")

    return call


def _member_dek(private, org, record, member_key_id):
    for wrap in record["payload"]["envelope"]["recipient_wraps"]:
        if wrap["member_key_id"] == member_key_id:
            return unwrap_secret(
                private,
                bytes.fromhex(wrap["wrapped_dek"]),
                f"dek\n{org}\n{member_key_id}\n{record['id']}\naudit".encode(),
            )
    return None


# H-05 ---------------------------------------------------------------------------------------


async def _approval_setup(client, monkeypatch, calls, policy_body):
    async def listed(endpoint, authorization=""):
        return [{"name": "pay.send", "description": "Send", "inputSchema": {"type": "object", "properties": {}}}]

    async def called(endpoint, name, arguments, authorization=""):
        calls["n"] += 1
        return {"content": [{"type": "text", "text": "ok"}], "tool": name, "echo": arguments}

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.saturn.services.saturn_mcp.call_remote_tool", called)
    connected = await client.post("/mcp-servers", json={"name": "Payments", "endpoint": "https://mcp.stripe.com/mcp", "authorization": "token-1"})
    tool_id = connected.json()["catalog"][0]["id"]
    created = await client.post("/tool-sets", json={"name": "Payments", "items": [{"tool_id": tool_id, "approval": True}]})
    people = await _people(client)
    groups = {}
    for name in ("Admin", "Finance"):
        groups[name] = (await client.post("/org/groups", json={"name": name})).json()["id"]
    policy = await client.post("/org/approval-policies", json=policy_body(groups))
    assert policy.status_code == 201, policy.text
    rule = await client.post(
        "/policies",
        json={"name": "HV", "rules": [{"tool": "pay.send", "condition": "true", "decision": "approval_required", "approval_policy_id": policy.json()["id"]}]},
    )
    await activate_created(client, rule)
    access = await oauth_access(client, "Pay Agent")
    await client.post("/mcp", headers={"Authorization": f"Bearer {access}"}, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    agent_id = next(item["id"] for item in (await client.get("/agents")).json() if item["name"] == "Pay Agent")
    assert (await client.patch(f"/agents/{agent_id}", json={"tool_set_id": created.json()["id"]})).status_code == 200
    return people, groups, access, agent_id


async def test_malformed_member_key_is_rejected_and_cannot_break_quorum_execution(client, monkeypatch):
    calls = {"n": 0}
    people, groups, access, agent_id = await _approval_setup(
        client,
        monkeypatch,
        calls,
        lambda g: {"name": "Two", "required_count": 2, "group_id": g["Finance"], "allow_requester": True},
    )
    for employee_id in (people[0][0], people[1][0], people[2][0]):
        await client.post(f"/org/groups/{groups['Finance']}/members", json={"employee_id": employee_id})
    body = await _request(client, access, agent_id, {"amount": 900})
    approval_id = body["error"]["data"]["approval_id"]

    registered = await client.post("/org/payload-keys/member", json={"public_key": BAD_KEY}, headers=_auth(people[3][1]))
    assert registered.status_code == 400, registered.text

    first = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[0][1]))
    assert first.status_code == 200, first.text
    second = await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[1][1]))
    assert second.status_code == 200, second.text
    assert calls["n"] == 1
    await client.post(f"/approvals/{approval_id}/approve", headers=_auth(people[2][1]))
    assert calls["n"] == 1
    plain = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access}", "X-Saturn-Agent-Id": agent_id},
        json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "pay.send", "arguments": {"amount": 1}}},
    )
    assert plain.status_code == 200, plain.text


async def test_malformed_org_key_is_rejected_at_creation_and_rotation(client):
    rejected = await client.post(
        "/org/payload-keys",
        json={"public_key": BAD_KEY, "backup": recovery_backup(os.urandom(32), "org", 1, os.urandom(64))},
    )
    assert rejected.status_code == 400, rejected.text
    await _org_key(client)
    rotate = await client.post("/org/payload-keys/rotate", json={"public_key": BAD_KEY})
    assert rotate.status_code == 400, rotate.text


async def test_stored_bad_member_key_is_skipped_and_flagged_not_fatal(client, monkeypatch):
    from app.core.ids import utcnow
    from app.models import MemberPayloadKey

    org_private, _org_public = await _org_key(client)
    org = await _org_id(client)
    generator, session = await _db(client)
    try:
        me = (await client.get("/auth/me")).json()
        session.add(
            MemberPayloadKey(
                id="mpk_legacy_bad",
                organization_id=org,
                user_id=me["id"],
                public_key=BAD_KEY,
                status="active",
                approval="approved",
                created_at=utcnow(),
            )
        )
        await session.commit()
    finally:
        await generator.aclose()
    call = await _gateway(client, monkeypatch)
    record = await call({"repo": "saturn"})
    envelope = record["payload"]["envelope"]
    assert envelope["wrapped_dek"], "the organization wrap must never be dropped"
    assert all(item["member_key_id"] != "mpk_legacy_bad" for item in envelope["recipient_wraps"])
    assert decrypt_payload(org_private, org, record["id"], "audit", envelope)["arguments"]["repo"] == "saturn"
    generator, session = await _db(client)
    try:
        flagged = await session.get(MemberPayloadKey, "mpk_legacy_bad")
        assert flagged.status == "invalid"
    finally:
        await generator.aclose()


async def test_stored_bad_org_key_fails_closed_instead_of_dropping_the_org(client):
    from fastapi import HTTPException

    from app.core.ids import utcnow
    from app.features.execution.activity.services.zero_access import protect_mapping
    from app.models import OrgPayloadKey

    org = await _org_id(client)
    generator, session = await _db(client)
    try:
        session.add(
            OrgPayloadKey(
                id="opk_bad",
                organization_id=org,
                version=1,
                public_key=BAD_KEY,
                algorithm="x",
                status="active",
                owner_user_id="u",
                created_at=utcnow(),
            )
        )
        await session.flush()
        with pytest.raises(HTTPException) as raised:
            await protect_mapping(session, org, "evt_x", "audit", {"arguments": {"a": 1}})
        assert raised.value.status_code == 503
        await session.rollback()
    finally:
        await generator.aclose()


# M-06 ---------------------------------------------------------------------------------------


async def test_member_key_needs_admin_approval_before_it_receives_wraps(client, monkeypatch):
    await _org_key(client)
    call = await _gateway(client, monkeypatch)
    people = await _people(client)
    viewer = _auth(people[3][1])
    private, public = generate_mlkem()
    registered = await client.post("/org/payload-keys/member", json={"public_key": public.hex()}, headers=viewer)
    assert registered.status_code == 200, registered.text
    assert registered.json()["approval"] == "pending"
    key_id = registered.json()["id"]
    own = await client.get("/org/payload-keys", headers=viewer)
    assert own.json()["own_member_key_approval"] == "pending"

    record = await call({"repo": "before"})
    assert all(item["member_key_id"] != key_id for item in record["payload"]["envelope"]["recipient_wraps"])

    assert (await client.get("/org/payload-keys/members/pending", headers=viewer)).status_code == 403
    assert (await client.post(f"/org/payload-keys/member/{key_id}/approve", headers=viewer)).status_code == 403
    pending = await client.get("/org/payload-keys/members/pending")
    assert pending.status_code == 200, pending.text
    assert [item["member_key_id"] for item in pending.json()["keys"]] == [key_id]

    approved = await client.post(f"/org/payload-keys/member/{key_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval"] == "approved"
    record = await call({"repo": "after"})
    org = await _org_id(client)
    assert _member_dek(private, org, record, key_id) is not None

    events = (await client.get("/org/payload-keys/audit")).json()["events"]
    names = [item["event"] for item in events]
    assert "payload_member_key_registered" in names
    assert "payload_member_key_approved" in names
    assert (await client.get("/org/payload-keys/audit", headers=viewer)).status_code == 403


async def test_admin_key_is_approved_on_registration(client):
    _private, public = generate_mlkem()
    registered = await client.post("/org/payload-keys/member", json={"public_key": public.hex()})
    assert registered.status_code == 200, registered.text
    assert registered.json()["approval"] == "approved"


async def test_rejected_key_gets_no_wraps(client, monkeypatch):
    await _org_key(client)
    call = await _gateway(client, monkeypatch)
    people = await _people(client)
    _private, public = generate_mlkem()
    key_id = (await client.post("/org/payload-keys/member", json={"public_key": public.hex()}, headers=_auth(people[0][1]))).json()["id"]
    assert (await client.post(f"/org/payload-keys/member/{key_id}/reject")).json()["approval"] == "rejected"
    assert (await client.post(f"/org/payload-keys/member/{key_id}/approve")).status_code == 409
    record = await call({"repo": "x"})
    assert all(item["member_key_id"] != key_id for item in record["payload"]["envelope"]["recipient_wraps"])


# M-07 ---------------------------------------------------------------------------------------


async def test_wraps_cannot_be_replaced_and_adding_needs_dek_proof(client, monkeypatch):
    await _org_key(client)
    org = await _org_id(client)
    admin_private, admin_public = generate_mlkem()
    admin_key = (await client.post("/org/payload-keys/member", json={"public_key": admin_public.hex()})).json()["id"]
    call = await _gateway(client, monkeypatch)
    record = await call({"repo": "saturn"})
    original = record["payload"]["envelope"]["recipient_wraps"]
    dek = _member_dek(admin_private, org, record, admin_key)
    assert dek is not None

    people = await _people(client)
    operator = _auth(people[0][1])
    op_private, op_public = generate_mlkem()
    op_key = (await client.post("/org/payload-keys/member", json={"public_key": op_public.hex()}, headers=operator)).json()["id"]
    garbage = "02" + "00" * 1628

    # Before approval nothing can be granted to the operator's key.
    early = await client.post(
        "/org/payload-keys/member/rewrap",
        json={"member_key_id": op_key, "records": [{"id": record["id"], "type": "audit", "wrapped_dek": garbage, "dek_proof": dek_proof(dek, org, record["id"], "audit")}]},
    )
    assert early.status_code == 409
    assert (await client.post(f"/org/payload-keys/member/{op_key}/approve")).status_code == 200

    # The operator tries to overwrite the admin's wrap: without the DEK, and even with a proof.
    for proof in ("", "00" * 32, dek_proof(dek, org, record["id"], "audit")):
        attack = await client.post(
            "/org/payload-keys/member/rewrap",
            json={"member_key_id": admin_key, "records": [{"id": record["id"], "type": "audit", "wrapped_dek": garbage, "dek_proof": proof}]},
            headers=operator,
        )
        assert attack.status_code == 409, attack.text
    # The public content_sha256 path does not work for new envelopes.
    attack = await client.post(
        "/org/payload-keys/member/rewrap",
        json={"member_key_id": op_key, "records": [{"id": record["id"], "type": "audit", "wrapped_dek": garbage, "content_sha256": "x"}]},
        headers=operator,
    )
    assert attack.status_code == 409
    after = next(item for item in (await client.get("/activity")).json() if item["id"] == record["id"])
    assert after["payload"]["envelope"]["recipient_wraps"] == original

    # A DEK holder (the admin) adds the operator's wrap.
    sources = await client.get("/org/payload-keys/member/rewrap", params={"member_key_id": op_key})
    assert sources.status_code == 200, sources.text
    assert sources.json()["next_cursor"] is None
    item = next(row for row in sources.json()["records"] if row["id"] == record["id"])
    assert item["proof_required"] is True
    own_dek = unwrap_secret(admin_private, bytes.fromhex(item["own_wrap"]), f"dek\n{org}\n{admin_key}\n{record['id']}\naudit".encode())
    wrapped = wrap_secret(op_public, own_dek, f"dek\n{org}\n{op_key}\n{record['id']}\naudit".encode()).hex()
    granted = await client.post(
        "/org/payload-keys/member/rewrap",
        json={"member_key_id": op_key, "records": [{"id": record["id"], "type": "audit", "wrapped_dek": wrapped, "dek_proof": dek_proof(own_dek, org, record["id"], "audit")}]},
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["migrated"] == 1
    after = next(item for item in (await client.get("/activity")).json() if item["id"] == record["id"])
    assert _member_dek(op_private, org, after, op_key) == dek
    # A second grant for the same key never replaces the first.
    again = await client.post(
        "/org/payload-keys/member/rewrap",
        json={"member_key_id": op_key, "records": [{"id": record["id"], "type": "audit", "wrapped_dek": garbage, "dek_proof": dek_proof(dek, org, record["id"], "audit")}]},
    )
    assert again.status_code == 409
    events = [item["event"] for item in (await client.get("/org/payload-keys/audit")).json()["events"]]
    assert "payload_wraps_granted" in events


async def test_rotation_rewrap_needs_owner_and_dek_proof(client, monkeypatch):
    old_private, _old_public = await _org_key(client)
    org = await _org_id(client)
    call = await _gateway(client, monkeypatch)
    record = await call({"repo": "saturn"})
    _new_private, new_public = generate_mlkem()
    assert (await client.post("/org/payload-keys/rotate", json={"public_key": new_public.hex()})).status_code == 200
    pending = (await client.get("/org/payload-keys/rotate/pending", params={"version": 1})).json()
    item = next(row for row in pending["records"] if row["id"] == record["id"])
    dek = unwrap_secret(old_private, bytes.fromhex(item["wrapped_dek"]), f"dek\n{org}\n1\n{record['id']}\naudit".encode())
    garbage = "02" + "00" * 1628
    no_proof = await client.post(
        "/org/payload-keys/rotate/batch",
        json={"version": 2, "records": [{"id": record["id"], "type": "audit", "wrapped_dek": garbage, "content_sha256": item["content_sha256"]}]},
    )
    assert no_proof.status_code == 409, no_proof.text
    unchanged = next(row for row in (await client.get("/activity")).json() if row["id"] == record["id"])
    assert unchanged["payload"]["envelope"]["org_key_version"] == 1

    people = await _people(client)
    generator, session = await _db(client)
    try:
        from sqlalchemy import select

        from app.models import Employee

        employee = await session.scalar(select(Employee).where(Employee.id == people[0][0]))
        employee.role = "admin"
        employee.permissions = {"agents": "write", "policies": "write", "approvals": "write", "team": "write"}
        from app.models import User

        (await session.get(User, employee.user_id)).totp_enabled = True
        await session.commit()
    finally:
        await generator.aclose()
    other_admin = await client.post(
        "/org/payload-keys/rotate/batch",
        json={"version": 2, "records": [{"id": record["id"], "type": "audit", "wrapped_dek": garbage, "dek_proof": dek_proof(dek, org, record["id"], "audit")}]},
        headers=_auth(people[0][1]),
    )
    assert other_admin.status_code == 403, other_admin.text
    assert "payload owner" in other_admin.text, other_admin.text


# L-27 ---------------------------------------------------------------------------------------


def test_new_envelopes_have_no_plain_content_hash_and_legacy_still_opens():
    private, public = generate_mlkem()
    content = {"arguments": {"pin": "1234"}}
    envelope = encrypt_payload(
        recipient_public=public,
        organization_id="org_a",
        record_id="evt_1",
        payload_type="audit",
        key_version=1,
        content=content,
    )
    assert "content_sha256" not in envelope
    assert content_hash(content) not in str(envelope)
    assert envelope["content_mac"] and envelope["key_check"]
    assert decrypt_payload(private, "org_a", "evt_1", "audit", envelope) == content
    forged = {**envelope, "content_mac": "00" * 32}
    with pytest.raises(Exception):
        decrypt_payload(private, "org_a", "evt_1", "audit", forged)

    legacy = {key: value for key, value in envelope.items() if key not in {"content_mac", "key_check", "integrity"}}
    legacy["content_sha256"] = content_hash(content)
    assert decrypt_payload(private, "org_a", "evt_1", "audit", legacy) == content
    with pytest.raises(Exception):
        decrypt_payload(private, "org_a", "evt_1", "audit", {**legacy, "content_sha256": "0" * 64})


# M-10 ---------------------------------------------------------------------------------------


async def test_member_backup_accepts_client_secret_blob_and_refuses_password_backups(client):
    _private, public = generate_mlkem()
    blob = os.urandom(200).hex()
    registered = await client.post(
        "/org/payload-keys/member",
        json={"public_key": public.hex(), "backup": {"v": 2, "kdf": "CLIENT-SECRET", "scheme": "passkey-prf-v1", "blob": blob}},
    )
    assert registered.status_code == 200, registered.text
    backup = (await client.get("/org/payload-keys/member/backup")).json()
    assert backup["blob"] == blob
    assert backup["scheme"] == "passkey-prf-v1"
    assert backup["legacy"] is False
    assert "salt" not in backup

    bad = await client.post(
        "/org/payload-keys/member",
        json={"public_key": public.hex(), "backup": {"v": 2, "kdf": "CLIENT-SECRET", "scheme": "bad scheme!", "blob": blob}},
    )
    assert bad.status_code == 400

    from app.features.execution.activity.services.zero_access import password_backup

    # The console now wraps member keys in the browser (CLIENT-SECRET), so the server refuses
    # backups it could open itself from the login password.
    private2, public2 = generate_mlkem()
    legacy = await client.post(
        "/org/payload-keys/member",
        json={"public_key": public2.hex(), "backup": password_backup("pw", "u", private2)},
    )
    assert legacy.status_code == 400, legacy.text
    assert (await client.get("/org/payload-keys/member/backup")).json()["blob"] == blob


# I-07 ---------------------------------------------------------------------------------------


async def test_recovery_backup_is_admin_only_and_stretched(client):
    await _org_key(client)
    people = await _people(client)
    assert (await client.get("/org/payload-keys/backup", headers=_auth(people[3][1]))).status_code == 403
    assert (await client.get("/org/payload-keys/backup", headers=_auth(people[0][1]))).status_code == 403
    backup = await client.get("/org/payload-keys/backup")
    assert backup.status_code == 200, backup.text
    assert backup.json()["kdf"] == "PBKDF2-HMAC-SHA256"
    assert backup.json()["iterations"] >= 600_000
    assert backup.json()["legacy"] is False


async def test_recovery_backup_rejects_weak_iterations(client):
    private, public = generate_mlkem()
    weak = recovery_backup(os.urandom(32), "org", 1, private)
    weak["iterations"] = 1000
    created = await client.post("/org/payload-keys", json={"public_key": public.hex(), "backup": weak})
    assert created.status_code == 400


def test_recovery_backup_reads_new_and_legacy_formats():
    private, _public = generate_mlkem()
    recovery = os.urandom(32)
    new = recovery_backup(recovery, "org_a", 1, private)
    assert new["kdf"] == "PBKDF2-HMAC-SHA256"
    assert open_recovery_backup(recovery, "org_a", new) == private
    old = legacy_recovery_backup(recovery, "org_a", 1, private)
    assert old["kdf"] == "HKDF-SHA256"
    assert open_recovery_backup(recovery, "org_a", old) == private
    with pytest.raises(Exception):
        open_recovery_backup(recovery, "org_a", {**new, "iterations": 1})


# L-29 ---------------------------------------------------------------------------------------


async def test_activity_is_filtered_in_sql_and_paginated(client, monkeypatch):
    call = await _gateway(client, monkeypatch)
    for index in range(3):
        await call({"repo": f"r{index}"})
    everything = (await client.get("/activity")).json()
    assert len(everything) >= 3
    first = await client.get("/activity", params={"limit": 1})
    assert first.status_code == 200
    assert len(first.json()) == 1
    assert first.headers["X-Next-Offset"] == "1"
    second = await client.get("/activity", params={"limit": 1, "offset": 1})
    assert second.json()[0]["id"] == everything[1]["id"]
    assert (await client.get("/activity", params={"limit": 100000})).status_code == 422
    assert all(row["tool"] == "repo.read" for row in (await client.get("/activity", params={"tool": "repo"})).json())
    assert (await client.get("/activity", params={"q": "100%_nothing"})).json() == []
    assert (await client.get("/activity", params={"q": "TOOL_REQ"})).json()
    exported = await client.get("/activity/export", params={"limit": 2})
    assert len(exported.json()) == 2
    assert exported.headers["X-Next-Offset"] == "2"


# H-08 ---------------------------------------------------------------------------------------


async def test_credential_ids_are_per_organization_without_an_existence_oracle(client):
    from app.features.credentials.vault.services.credential_service import credential_service
    from app.models import Organization
    from app.schemas import CredentialWrite

    created = await client.post(
        "/org/credentials",
        json={"id": "crd_shared", "kind": "key", "name": "Mine", "secret_name": "API_KEY", "secret": "s3cret"},
    )
    assert created.status_code == 200, created.text
    org = await _org_id(client)
    generator, session = await _db(client)
    try:
        session.add(Organization(id="org_other", name="Other"))
        await session.flush()
        other = SimpleNamespace(organization_id="org_other", id="user_other")
        row = await credential_service.upsert(
            session,
            other,
            CredentialWrite(id="crd_shared", kind="key", name="Theirs", secret_name="API_KEY", secret="other-secret"),
        )
        assert row.organization_id == "org_other"
        assert row.vault_id and row.vault_id != created.json().get("vault_id")
        await session.commit()
        await credential_service.delete(session, other, "crd_shared")
        await session.commit()
    finally:
        await generator.aclose()
    listed = await client.get("/org/credentials")
    mine = next(item for item in listed.json() if item["id"] == "crd_shared")
    assert mine["name"] == "Mine" and mine["has_secret"] is True
    generator, session = await _db(client)
    try:
        from app.features.credentials.vault.services.secret_vault import get_secret
        from app.models import OrgCredential

        row = await session.get(OrgCredential, {"organization_id": org, "id": "crd_shared"})
        assert await get_secret(session, row.vault_id, org) == "s3cret"
    finally:
        await generator.aclose()


def test_secret_manager_names_are_unique_per_org_and_id():
    from app.features.credentials.vault.services.secret_vault import _gcp_name

    names = {
        _gcp_name("org-crd_abc", "org_a"),
        _gcp_name("org-crd-abc", "org_a"),
        _gcp_name("org-crd.abc", "org_a"),
        _gcp_name("-org-crd_abc", "org_a"),
        _gcp_name("org-crd_abc", "org_b"),
    }
    assert len(names) == 5
    assert all(len(name) <= 255 and name.replace("-", "").replace("_", "").isalnum() for name in names)


async def test_secret_manager_reads_legacy_name_and_never_deletes_another_tenants_entry(client, monkeypatch):
    from app.features.credentials.vault.services import secret_vault
    from app.features.credentials.vault.services.pq_crypto import seal

    store = {secret_vault._legacy_gcp_name("org-crd_x"): seal("old-value", "org_a")}
    deleted = []
    monkeypatch.setattr(secret_vault, "_project", lambda: "proj")
    monkeypatch.setattr(secret_vault, "_gcp_get", lambda project, name: store.get(name, ""))
    monkeypatch.setattr(secret_vault, "_gcp_put", lambda project, name, blob: store.__setitem__(name, blob))

    def drop(project, name):
        deleted.append(name)
        store.pop(name, None)

    monkeypatch.setattr(secret_vault, "_gcp_delete", drop)
    generator, session = await _db(client)
    try:
        assert await secret_vault.get_secret(session, "org-crd_x", "org_a") == "old-value"
        # Another tenant writing the colliding id gets its own name and leaves org_a's entry alone.
        await secret_vault.put_secret(session, "org-crd_x", "intruder", "org_b")
        await secret_vault.delete_secret(session, "org-crd_x", "org_b")
        assert secret_vault._legacy_gcp_name("org-crd_x") not in deleted
        assert await secret_vault.get_secret(session, "org-crd_x", "org_a") == "old-value"
        # The owner rewriting it migrates to the new name and removes its own legacy entry.
        await secret_vault.put_secret(session, "org-crd_x", "new-value", "org_a")
        assert secret_vault._legacy_gcp_name("org-crd_x") in deleted
        assert await secret_vault.get_secret(session, "org-crd_x", "org_a") == "new-value"
    finally:
        await generator.aclose()


# Audit 2026-11-08 -----------------------------------------------------------------------------


async def test_replacing_a_member_key_needs_a_recent_sign_in(client, monkeypatch):
    from datetime import timedelta

    _private, public = generate_mlkem()
    assert (await client.post("/org/payload-keys/member", json={"public_key": public.hex()})).status_code == 200
    monkeypatch.setattr("app.features.identity.auth.services.auth_service.RECENT_AUTH", timedelta(seconds=-1))
    _private, stolen = generate_mlkem()
    blocked = await client.post("/org/payload-keys/member", json={"public_key": stolen.hex(), "needs_assist": True})
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "Reauthentication required"
    own = await client.get("/org/payload-keys")
    assert own.json()["own_member_key_approval"] == "approved"


async def test_an_admin_replacement_key_waits_for_another_admin(client):
    # The seeded admin and a second admin each hold an approved key.
    _private, first = generate_mlkem()
    assert (await client.post("/org/payload-keys/member", json={"public_key": first.hex()})).json()["approval"] == "approved"
    invited = await client.post("/org/invites", json={"email": "second-admin@saturn.ai", "name": "Second", "role": "admin"})
    assert invited.status_code == 201, invited.text
    accepted = await client.post("/auth/accept-invite", json={"token": invited.json()["token"], "password": "password1"})
    assert accepted.status_code == 200, accepted.text
    from tests.totp_access import finish_totp

    second = _auth(accepted.json()["token"])
    await finish_totp(client, second)
    _private, theirs = generate_mlkem()
    assert (await client.post("/org/payload-keys/member", json={"public_key": theirs.hex()}, headers=second)).json()["approval"] == "approved"

    # The first admin's replacement is held, and they cannot approve it themselves.
    _private, replacement = generate_mlkem()
    replaced = await client.post("/org/payload-keys/member", json={"public_key": replacement.hex(), "needs_assist": True})
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["approval"] == "pending"
    key_id = replaced.json()["id"]
    assert (await client.post(f"/org/payload-keys/member/{key_id}/approve")).status_code == 403
    approved = await client.post(f"/org/payload-keys/member/{key_id}/approve", headers=second)
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval"] == "approved"


async def test_a_sole_admin_can_still_replace_their_key(client):
    _private, first = generate_mlkem()
    assert (await client.post("/org/payload-keys/member", json={"public_key": first.hex()})).json()["approval"] == "approved"
    _private, replacement = generate_mlkem()
    replaced = await client.post("/org/payload-keys/member", json={"public_key": replacement.hex()})
    assert replaced.json()["approval"] == "approved"

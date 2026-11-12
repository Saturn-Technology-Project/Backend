import os

import pytest

from app.features.execution.activity.services.zero_access import (
    dek_proof,
    decrypt_payload,
    encrypt_payload,
    generate_mlkem,
    open_password_backup,
    open_recovery_backup,
    password_backup,
    recovery_backup,
    unwrap_secret,
    wrap_secret,
    member_wrap_aad,
)
from tests.oauth_access import oauth_access


def test_login_verifier_cannot_open_the_member_key():
    from app.features.identity.auth.services.auth_service import login_secret

    private, public = generate_mlkem()
    backup = password_backup("correct horse", "user_1", private)
    assert open_password_backup("correct horse", "user_1", backup) == private
    with pytest.raises(Exception):
        open_password_backup(login_secret("correct horse"), "user_1", backup)


def test_member_password_backup_and_recipient_wrap():
    private, public = generate_mlkem()
    envelope = encrypt_payload(
        recipient_public=public,
        organization_id="org_a",
        record_id="evt_1",
        payload_type="audit",
        key_version=1,
        content={"arguments": {"note": "secret"}},
        member_keys=[("user_1", "mpk_1", public)],
    )
    wrapped = bytes.fromhex(envelope["recipient_wraps"][0]["wrapped_dek"])
    dek = unwrap_secret(private, wrapped, b"dek\norg_a\nmpk_1\nevt_1\naudit")
    assert len(dek) == 32
    backup = password_backup("correct horse", "user_1", private)
    assert "correct horse" not in str(backup)
    assert open_password_backup("correct horse", "user_1", backup) == private
    with pytest.raises(Exception):
        open_password_backup("wrong horse", "user_1", backup)


def test_payload_envelope_rejects_tampering():
    private, public = generate_mlkem()
    envelope = encrypt_payload(
        recipient_public=public,
        organization_id="org_a",
        record_id="evt_1",
        payload_type="audit",
        key_version=1,
        content={"arguments": {"note": "secret"}},
    )
    opened = decrypt_payload(private, "org_a", "evt_1", "audit", envelope)
    assert opened["arguments"]["note"] == "secret"
    other, _public = generate_mlkem()
    with pytest.raises(Exception):
        decrypt_payload(other, "org_a", "evt_1", "audit", envelope)
    broken = {**envelope, "ciphertext": ("00" + envelope["ciphertext"][2:])}
    with pytest.raises(Exception):
        decrypt_payload(private, "org_a", "evt_1", "audit", broken)
    with pytest.raises(Exception):
        decrypt_payload(private, "org_b", "evt_1", "audit", envelope)
    wrapped = bytes.fromhex(envelope["wrapped_dek"])
    with pytest.raises(Exception):
        unwrap_secret(private, wrapped, b"dek\norg_a\n1\nevt_1\nother")


def test_recovery_key_restores_the_private_key_locally():
    private, _public = generate_mlkem()
    recovery = os.urandom(32)
    backup = recovery_backup(recovery, "org_a", 1, private)
    assert open_recovery_backup(recovery, "org_a", backup) == private
    with pytest.raises(Exception):
        open_recovery_backup(os.urandom(32), "org_a", backup)
    assert "recovery_key" not in backup


def test_member_envelope_is_bound_to_the_member():
    org_private, org_public = generate_mlkem()
    member_private, member_public = generate_mlkem()
    aad = member_wrap_aad("org_a", "user_a", 1)
    wrapped = wrap_secret(member_public, org_private, aad)
    assert unwrap_secret(member_private, wrapped, aad) == org_private
    with pytest.raises(Exception):
        unwrap_secret(member_private, wrapped, member_wrap_aad("org_a", "user_b", 1))
    assert org_public != member_public


async def test_gateway_stores_ciphertext_and_admin_cannot_read_it(client, monkeypatch):
    org_private, org_public = generate_mlkem()
    recovery = os.urandom(32)
    created = await client.post(
        "/org/payload-keys",
        json={"public_key": org_public.hex(), "backup": recovery_backup(recovery, "ignored", 1, org_private)},
    )
    assert created.status_code == 200, created.text
    rejected = await client.post(
        "/org/payload-keys",
        json={"public_key": org_public.hex(), "backup": recovery_backup(recovery, "x", 1, org_private), "recovery_key": recovery.hex()},
    )
    assert rejected.status_code == 422

    async def listed(endpoint, authorization=""):
        return [{"name": "repo.read", "description": "Read", "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}}}}]

    async def called(endpoint, name, arguments, authorization=""):
        return {"content": [{"type": "text", "text": "ok"}], "echo": arguments}

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.saturn.services.saturn_mcp.call_remote_tool", called)
    connected = await client.post(
        "/mcp-servers",
        json={"name": "GitHub", "endpoint": "https://api.githubcopilot.com/mcp", "authorization": "token-1"},
    )
    tool_id = next(item["id"] for item in connected.json()["catalog"] if item["name"] == "repo.read")
    tool_set = await client.post("/tool-sets", json={"name": "Reads", "items": [{"tool_id": tool_id, "approval": False}]})
    access = await oauth_access(client, "Zero Agent")
    await client.post("/mcp", headers={"Authorization": f"Bearer {access}"}, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    agent_id = next(item["id"] for item in (await client.get("/agents")).json() if item["name"] == "Zero Agent")
    await client.patch(f"/agents/{agent_id}", json={"tool_set_id": tool_set.json()["id"]})
    sent = await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access}", "X-Saturn-Agent-Id": agent_id},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "repo.read", "arguments": {"repo": "saturn", "note": "secret note"}}},
    )
    assert sent.status_code == 200, sent.text
    activity = await client.get("/activity")
    requested = next(item for item in activity.json() if item["tool"] == "repo.read" and item["event"] == "tool_requested")
    assert requested["payload"]["sealed"] is True
    assert "secret note" not in str(requested["payload"])
    envelope = requested["payload"]["envelope"]
    org = (await client.get("/org")).json()["organization"]["id"]
    opened = decrypt_payload(org_private, org, requested["id"], "audit", envelope)
    assert opened["arguments"]["note"] == "secret note"
    revealed = await client.post(f"/activity/{requested['id']}/reveal")
    assert revealed.status_code == 200
    assert "secret note" not in revealed.text
    admin = await client.get("/admin/audit")
    admin_row = next(item for item in admin.json() if item["id"] == requested["id"])
    assert "envelope" not in (admin_row["payload"] or {})
    assert "secret note" not in str(admin_row["payload"])


async def test_rotation_rewraps_the_dek_and_leaves_ciphertext_unchanged(client, monkeypatch):
    old_private, old_public = generate_mlkem()
    new_private, new_public = generate_mlkem()
    recovery = os.urandom(32)
    created = await client.post(
        "/org/payload-keys",
        json={"public_key": old_public.hex(), "backup": recovery_backup(recovery, "org", 1, old_private)},
    )
    assert created.status_code == 200, created.text

    async def listed(endpoint, authorization=""):
        return [{"name": "repo.read", "description": "Read", "inputSchema": {"type": "object"}}]

    async def called(endpoint, name, arguments, authorization=""):
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr("app.features.mcp.servers.services.mcp_connect.list_remote_tools", listed)
    monkeypatch.setattr("app.features.mcp.saturn.services.saturn_mcp.call_remote_tool", called)
    connected = await client.post(
        "/mcp-servers",
        json={"name": "GitHub", "endpoint": "https://api.githubcopilot.com/mcp", "authorization": "token-1"},
    )
    tool_id = connected.json()["catalog"][0]["id"]
    tool_set = await client.post("/tool-sets", json={"name": "Rotate", "items": [{"tool_id": tool_id, "approval": False}]})
    access = await oauth_access(client, "Rotate Agent")
    await client.post("/mcp", headers={"Authorization": f"Bearer {access}"}, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    agent_id = next(item["id"] for item in (await client.get("/agents")).json() if item["name"] == "Rotate Agent")
    await client.patch(f"/agents/{agent_id}", json={"tool_set_id": tool_set.json()["id"]})
    await client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {access}", "X-Saturn-Agent-Id": agent_id},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "repo.read", "arguments": {"repo": "saturn"}}},
    )
    requested = next(item for item in (await client.get("/activity")).json() if item["event"] == "tool_requested" and item["tool"] == "repo.read")
    envelope = requested["payload"]["envelope"]
    original_ciphertext = envelope["ciphertext"]
    org = (await client.get("/org")).json()["organization"]["id"]
    dek = unwrap_secret(
        old_private,
        bytes.fromhex(envelope["wrapped_dek"]),
        f"dek\n{org}\n1\n{requested['id']}\naudit".encode(),
    )
    assert len(dek) == 32
    started = await client.post("/org/payload-keys/rotate", json={"public_key": new_public.hex()})
    assert started.status_code == 200, started.text
    pending = await client.get("/org/payload-keys/rotate/pending", params={"version": 1})
    assert pending.status_code == 200, pending.text
    records = []
    for item in pending.json()["records"]:
        dek = unwrap_secret(
            old_private,
            bytes.fromhex(item["wrapped_dek"]),
            f"dek\n{org}\n1\n{item['id']}\n{item['type']}".encode(),
        )
        records.append(
            {
                "id": item["id"],
                "type": item["type"],
                "wrapped_dek": wrap_secret(new_public, dek, f"dek\n{org}\n2\n{item['id']}\n{item['type']}".encode()).hex(),
                "dek_proof": dek_proof(dek, org, item["id"], item["type"]),
            }
        )
    assert records
    batch = await client.post("/org/payload-keys/rotate/batch", json={"version": 2, "records": records})
    assert batch.status_code == 200, batch.text
    again = await client.post("/org/payload-keys/rotate/batch", json={"version": 2, "records": records})
    assert again.status_code == 200, again.text
    activated = await client.post(
        "/org/payload-keys/rotate/activate",
        json={"backup": recovery_backup(os.urandom(32), org, 2, new_private)},
    )
    assert activated.status_code == 200, activated.text
    refreshed = next(item for item in (await client.get("/activity")).json() if item["id"] == requested["id"])
    assert refreshed["payload"]["envelope"]["ciphertext"] == original_ciphertext
    assert refreshed["payload"]["envelope"]["org_key_version"] == 2
    opened = decrypt_payload(new_private, org, requested["id"], "audit", refreshed["payload"]["envelope"])
    assert opened["arguments"]["repo"] == "saturn"
    names = [item["event"] for item in (await client.get("/org/payload-keys/audit")).json()["events"]]
    assert names.count("payload_rotation_started") == 1
    assert names.count("payload_rotation_activated") == 1


async def test_member_cannot_read_another_members_envelope(client):
    _private, public = generate_mlkem()
    registered = await client.post("/org/payload-keys/member", json={"public_key": public.hex()})
    assert registered.status_code == 200, registered.text
    missing = await client.get("/org/payload-keys/envelopes/user_other")
    assert missing.status_code == 404

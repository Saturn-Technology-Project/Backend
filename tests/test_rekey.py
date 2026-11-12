"""A password or email change carries the re-wrapped member key and the new password value."""

import hashlib
import os

from app.features.execution.activity.services.zero_access import generate_mlkem, recovery_backup


def _verifier(password: str, email: str) -> str:
    # The console sends a value derived from the password and the email. The exact derivation is
    # the browser's; what matters here is that a different email gives a different value.
    return "v2." + hashlib.sha256(f"{email.lower()}\n{password}".encode()).hexdigest()


def _backup(tag: str) -> dict:
    return {"v": 2, "kdf": "CLIENT-SECRET", "scheme": "argon2id-hkdf-aesgcm-v2", "blob": f"{tag * 12}.{tag * 40}"}


async def _account(client, monkeypatch, email="member@saturn.ai", password="correct horse"):
    invited = await client.post("/org/invites", json={"email": email, "name": "Member", "role": "operator"})
    assert invited.status_code == 201, invited.text
    accepted = await client.post(
        "/auth/accept-invite", json={"token": invited.json()["token"], "password": _verifier(password, email)}
    )
    assert accepted.status_code == 200, accepted.text
    headers = {"Authorization": f"Bearer {accepted.json()['token']}"}
    private, public = generate_mlkem()
    created = await client.post(
        "/org/payload-keys", json={"public_key": public.hex(), "backup": recovery_backup(os.urandom(32), "org", 1, private)}
    )
    assert created.status_code == 200, created.text
    _mine, member_public = generate_mlkem()
    registered = await client.post(
        "/org/payload-keys/member", json={"public_key": member_public.hex(), "backup": _backup("aa")}, headers=headers
    )
    assert registered.status_code == 200, registered.text
    return headers, registered.json()["id"]


async def _stored_blob(client, headers) -> tuple[str, str]:
    backup = await client.get("/org/payload-keys/member/backup", headers=headers)
    assert backup.status_code == 200, backup.text
    return backup.json()["member_key_id"], backup.json()["blob"]


async def test_a_password_change_replaces_the_key_backup_in_the_same_request(client, monkeypatch):
    email = "member@saturn.ai"
    headers, key_id = await _account(client, monkeypatch)
    changed = await client.patch(
        "/auth/password",
        headers=headers,
        json={
            "current_password": _verifier("correct horse", email),
            "new_password": _verifier("battery staple", email),
            "member_backup": _backup("bb"),
        },
    )
    assert changed.status_code == 204, changed.text
    # The same key, with the copy wrapped under the new password. No new key was registered.
    assert await _stored_blob(client, headers) == (key_id, _backup("bb")["blob"])
    login = await client.post("/auth/login", json={"email": email, "password": _verifier("battery staple", email)})
    assert login.status_code == 200, login.text


async def test_a_wrong_current_password_changes_neither_the_password_nor_the_backup(client, monkeypatch):
    email = "member@saturn.ai"
    headers, key_id = await _account(client, monkeypatch)
    refused = await client.patch(
        "/auth/password",
        headers=headers,
        json={"current_password": _verifier("wrong", email), "new_password": _verifier("battery staple", email), "member_backup": _backup("bb")},
    )
    assert refused.status_code == 401, refused.text
    assert await _stored_blob(client, headers) == (key_id, _backup("aa")["blob"])


async def test_a_bad_backup_refuses_the_whole_password_change(client, monkeypatch):
    email = "member@saturn.ai"
    headers, key_id = await _account(client, monkeypatch)
    bad = {"v": 2, "kdf": "CLIENT-SECRET", "scheme": "not a scheme!", "blob": "x"}
    refused = await client.patch(
        "/auth/password",
        headers=headers,
        json={"current_password": _verifier("correct horse", email), "new_password": _verifier("battery staple", email), "member_backup": bad},
    )
    assert refused.status_code == 400, refused.text
    # The old password still signs in: the password was not changed while the backup was refused.
    assert (await client.post("/auth/login", json={"email": email, "password": _verifier("correct horse", email)})).status_code == 200
    assert await _stored_blob(client, headers) == (key_id, _backup("aa")["blob"])


async def _start_email_change(client, monkeypatch, headers, new_email):
    sent = []

    async def capture(email, code, kind, _locale=None):
        sent.append((email, code, kind))

    monkeypatch.setattr("app.features.identity.auth.controllers.auth.send_email_change_code", capture)
    started = await client.patch("/auth/me", headers=headers, json={"name": "Member", "email": new_email})
    assert started.status_code == 200, started.text
    current = next(code for _email, code, kind in sent if kind == "email_current")
    nxt = next(code for email, code, kind in sent if kind == "email_next" and email == new_email)
    return current, nxt


async def test_an_email_change_keeps_the_password_working(client, monkeypatch):
    old, new = "member@saturn.ai", "moved@saturn.ai"
    headers, key_id = await _account(client, monkeypatch)
    current, nxt = await _start_email_change(client, monkeypatch, headers, new)
    done = await client.post(
        "/auth/email/confirm",
        headers=headers,
        json={
            "current_code": current,
            "next_code": nxt,
            "current_password": _verifier("correct horse", old),
            "new_password": _verifier("correct horse", new),
            "member_backup": _backup("cc"),
        },
    )
    assert done.status_code == 200, done.text
    assert done.json()["email"] == new
    # The value the console derives for the new address signs in. Before, only the value for the
    # old address was stored, which the console could no longer produce.
    assert (await client.post("/auth/login", json={"email": new, "password": _verifier("correct horse", new)})).status_code == 200
    assert (await client.post("/auth/login", json={"email": new, "password": _verifier("correct horse", old)})).status_code == 401
    assert await _stored_blob(client, headers) == (key_id, _backup("cc")["blob"])


async def test_an_email_change_with_a_wrong_password_does_not_change_the_email(client, monkeypatch):
    old, new = "member@saturn.ai", "moved@saturn.ai"
    headers, key_id = await _account(client, monkeypatch)
    current, nxt = await _start_email_change(client, monkeypatch, headers, new)
    refused = await client.post(
        "/auth/email/confirm",
        headers=headers,
        json={"current_code": current, "next_code": nxt, "current_password": _verifier("wrong", old), "new_password": _verifier("correct horse", new)},
    )
    assert refused.status_code == 401, refused.text
    assert (await client.get("/auth/me", headers=headers)).json()["email"] == old
    assert await _stored_blob(client, headers) == (key_id, _backup("aa")["blob"])
    # The codes were not spent by the refusal, so the member can try again with the right password.
    done = await client.post(
        "/auth/email/confirm",
        headers=headers,
        json={"current_code": current, "next_code": nxt, "current_password": _verifier("correct horse", old), "new_password": _verifier("correct horse", new)},
    )
    assert done.status_code == 200, done.text


async def test_an_email_change_without_a_password_still_works_as_before(client, monkeypatch):
    old, new = "member@saturn.ai", "moved@saturn.ai"
    headers, _key_id = await _account(client, monkeypatch)
    current, nxt = await _start_email_change(client, monkeypatch, headers, new)
    done = await client.post("/auth/email/confirm", headers=headers, json={"current_code": current, "next_code": nxt})
    assert done.status_code == 200, done.text
    assert done.json()["email"] == new

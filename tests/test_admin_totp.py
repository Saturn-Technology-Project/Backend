from sqlalchemy import select

from app.core.config import get_settings
from app.features.identity.auth.services.totp import current_code, hash_backup_code


async def _session():
    from app.db.database import get_session
    from app.main import app

    override = app.dependency_overrides[get_session]
    generator = override()
    session = await anext(generator)
    return session, generator


async def test_admin_without_totp_is_blocked_until_setup(client):
    from app.models import TotpBackupCode, User

    session, generator = await _session()
    try:
        user = await session.get(User, get_settings().default_actor_id)
        user.totp_enabled = False
        user.totp_secret = ""
        await session.commit()
    finally:
        await generator.aclose()

    blocked = await client.get("/admin/orgs")
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "MFA setup is required"
    me = await client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["mfa_setup_required"] is True

    setup = await client.post("/auth/totp/setup")
    assert setup.status_code == 200, setup.text
    body = setup.json()
    assert body["secret"]
    assert body["otpauth_url"].startswith("otpauth://totp/")
    assert len(body["backup_codes"]) == 8

    session, generator = await _session()
    try:
        stored = list(
            (
                await session.scalars(
                    select(TotpBackupCode).where(TotpBackupCode.user_id == get_settings().default_actor_id)
                )
            ).all()
        )
        assert stored
        for code in body["backup_codes"]:
            assert all(code not in row.code_hash for row in stored)
            assert hash_backup_code(code) in {row.code_hash for row in stored}
    finally:
        await generator.aclose()

    confirmed = await client.post("/auth/totp/confirm", json={"code": current_code(body["secret"])})
    assert confirmed.status_code == 204, confirmed.text
    allowed = await client.get("/admin/orgs")
    assert allowed.status_code == 200, allowed.text
    finished = await client.get("/auth/me")
    assert finished.json()["mfa_setup_required"] is False

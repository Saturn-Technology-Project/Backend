"""Code checks count each guess atomically and are rate limited (audit 2026-11-08)."""

from datetime import timedelta

from sqlalchemy import update

from app.core.ids import new_id, utcnow
from app.db.database import get_session
from app.features.identity.auth.services.attempts import reserve_attempt
from app.features.identity.auth.services.auth_service import hash_password
from app.main import app
from app.models import PasswordReset


async def _db():
    generator = app.dependency_overrides[get_session]()
    session = await anext(generator)
    return generator, session


async def test_parallel_guesses_cannot_share_one_attempt(client):
    generator, session = await _db()
    try:
        record = PasswordReset(
            id=new_id("pwr"),
            email="someone@example.com",
            code_hash=hash_password("123456"),
            expires_at=utcnow() + timedelta(minutes=10),
            attempts=0,
        )
        session.add(record)
        await session.flush()
        # Four other requests counted their guesses after this one loaded the row.
        await session.execute(
            update(PasswordReset).where(PasswordReset.id == record.id).values(attempts=4).execution_options(synchronize_session=False)
        )
        assert record.attempts == 0  # what this request read
        assert await reserve_attempt(session, record, "attempts", 5) == 5
        assert await reserve_attempt(session, record, "attempts", 5) is None
        assert record.attempts == 5
    finally:
        await generator.aclose()


async def test_reset_code_is_refused_once_the_cap_is_spent_behind_its_back(client):
    requested = await client.post("/auth/forgot-password", json={"email": "rick@saturn.ai"})
    assert requested.status_code == 204
    generator, session = await _db()
    try:
        await session.execute(
            update(PasswordReset).where(PasswordReset.email == "rick@saturn.ai").values(attempts=5)
        )
        await session.commit()
    finally:
        await generator.aclose()
    tried = await client.post(
        "/auth/reset-password", json={"email": "rick@saturn.ai", "code": "000000", "password": "a-new-password-1"}
    )
    assert tried.status_code == 400


async def test_code_checks_are_limited_per_address(client):
    statuses = []
    for _ in range(32):
        response = await client.post("/auth/login/email", json={"challenge": "nope", "code": "000000"})
        statuses.append(response.status_code)
    assert statuses[0] == 400
    assert statuses[-1] == 429


async def test_reset_attempts_are_limited_per_account(client):
    statuses = []
    for n in range(17):
        response = await client.post(
            "/auth/reset-password",
            headers={"X-Forwarded-For": f"10.1.0.{n}"},
            json={"email": "Victim@Example.com", "code": "000000", "password": "a-new-password-1"},
        )
        statuses.append(response.status_code)
    assert statuses[0] == 400
    assert statuses[-1] == 429

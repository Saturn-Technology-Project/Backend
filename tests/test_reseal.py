import hashlib
import hmac

from sqlalchemy import select

from app.cli.reseal import Keys, main, reseal
from app.features.credentials.vault.services import pq_crypto

OLD_ROOT = b"\x11" * 32  # the root conftest installs
NEW_ROOT = b"\x22" * 32


def _local(root: bytes):
    return lambda data: hmac.new(root, data, hashlib.sha256).digest()


def test_keys_derive_what_pq_crypto_derives():
    from app.features.execution.activity.services.content_seal import open_json, seal_json
    from app.features.execution.approvals.services.approval_arguments import keyed_digest

    keys = Keys(_local(OLD_ROOT))
    assert pq_crypto.open_secret(keys.seal("token", "org_a", "user_a"), "org_a", "user_a") == "token"
    assert keys.open(pq_crypto.seal("token", "org_a", ""), "org_a", "") == "token"
    assert keys.open(pq_crypto.seal("token", "org_a", ""), "org_b", "") is None
    assert open_json("org_a", keys.seal_content(b'{"a":1}', "org_a")) == {"a": 1}
    assert keys.open_content(seal_json("org_a", {"a": 1}), "org_a") == b'{"a":1}'
    assert keys.digest("org_a", {"amount": 5}) == keyed_digest("org_a", {"amount": 5})


async def test_reseal_moves_every_sealed_value_to_the_new_key(client):
    from app.core.ids import utcnow
    from app.db.database import get_session
    from app.main import app
    from app.models import SecretVault, User
    from app.features.execution.activity.services.content_seal import seal_json

    override = app.dependency_overrides[get_session]
    generator = override()
    session = await anext(generator)
    try:
        user = (await session.scalars(select(User).where(User.email == "rick@saturn.ai"))).one()
        organization_id, user_id = user.organization_id, user.id
        user.totp_secret = pq_crypto.seal("JBSWY3DPEHPK3PXP", organization_id, user_id)
        # The vault table does not name its tenant; the tool finds it by trying each one.
        session.add(SecretVault(id="vault-org", ciphertext=pq_crypto.seal("org-secret", organization_id), updated_at=utcnow()))
        session.add(SecretVault(id="vault-content", ciphertext=seal_json(organization_id, {"q": "x"}), updated_at=utcnow()))
        await session.commit()

        old, new = Keys(_local(OLD_ROOT)), Keys(_local(NEW_ROOT))
        connection = await session.connection()
        dry = await reseal(connection, old, new, apply=False)
        assert dry.resealed == {"users": 1, "secret_vault": 2} and not dry.unopened
        await session.rollback()
        assert pq_crypto.open_secret((await session.get(User, user_id)).totp_secret, organization_id, user_id)

        connection = await session.connection()
        report = await reseal(connection, old, new, apply=True)
        await session.commit()
        assert report.resealed == {"users": 1, "secret_vault": 2} and not report.unopened

        session.expire_all()
        totp = (await session.get(User, user_id)).totp_secret
        vault = (await session.get(SecretVault, "vault-org")).ciphertext
        content = (await session.get(SecretVault, "vault-content")).ciphertext
        assert old.open(totp, organization_id, user_id) is None
        assert new.open(totp, organization_id, user_id) == "JBSWY3DPEHPK3PXP"
        assert new.open(vault, organization_id, "") == "org-secret"
        assert new.open_content(content, organization_id) == b'{"q":"x"}'

        # A second run finds nothing the old key opens, and refuses to pretend otherwise.
        again = await reseal(await session.connection(), old, new, apply=False)
        assert again.resealed == {} and len(again.unopened) == 3
        await session.rollback()
    finally:
        await generator.aclose()


def test_command_refuses_the_same_key_twice(capsys):
    try:
        main(["--old-key", "k/1", "--new-key", "k/1"])
    except SystemExit as exit:
        assert exit.code == 2
    assert "the same" in capsys.readouterr().err


async def test_tenant_text_that_looks_sealed_does_not_block_a_reseal(client):
    from app.db.database import get_session
    from app.main import app
    from app.models import User

    from app.cli.reseal import sealed_location

    assert sealed_location("secret_vault vault-1 ciphertext")
    assert sealed_location("users u1 totp_secret")
    assert sealed_location("audit_logs a1 payload.sealed")
    assert not sealed_location("users u1 name")
    assert not sealed_location("agents a1 settings.instructions")

    generator = app.dependency_overrides[get_session]()
    session = await anext(generator)
    try:
        user = (await session.scalars(select(User).where(User.email == "rick@saturn.ai"))).one()
        user.name = "pq2.not-really-sealed"
        await session.commit()
        report = await reseal(await session.connection(), Keys(_local(OLD_ROOT)), Keys(_local(NEW_ROOT)), apply=False)
        assert not report.unopened
        assert any(item.endswith(" name") for item in report.ignored)
        await session.rollback()
    finally:
        await generator.aclose()

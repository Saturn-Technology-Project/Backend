def test_verification_email_uses_one_language():
    from app.features.identity.auth.services.mailer import _code_message, mail_locale

    assert mail_locale("en-US,en;q=0.9") == "en"
    assert mail_locale("zh-TW,zh;q=0.9,en;q=0.8") == "zh"
    assert mail_locale(None) == "zh"

    zh_subject, zh_text, zh_html = _code_message("482193", "signup", "zh")
    en_subject, en_text, en_html = _code_message("482193", "signup", "en")
    assert zh_subject == "482193 是你的 Saturn 帳號確認碼"
    assert "確認你的帳號" in zh_text
    assert "Confirm your account" not in zh_text
    assert 'lang="zh-Hant"' in zh_html
    assert en_subject == "482193 is your Saturn confirmation code"
    assert "Confirm your account" in en_text
    assert "確認你的帳號" not in en_text
    assert 'lang="en"' in en_html
    assert "482193" in zh_html and "482193" in en_html
    assert "<script" not in zh_html


def test_reset_code_hash_fits_column():
    from app.features.identity.auth.models.password_reset import PasswordReset
    from app.features.identity.auth.services.auth_service import hash_password

    stored = hash_password("123456")
    limit = PasswordReset.__table__.c.code_hash.type.length
    assert len(stored) <= limit


async def test_login_and_me(client):
    me = await client.get("/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["id"] == "user_operator"
    assert body["email"] == "rick@saturn.ai"
    assert body["organization"]["id"] == "org_saturn"
    assert body["enterprise"]["id"] == "org_saturn"
    assert body["role"] == "admin"
    assert body["permissions"]["team"] == "write"


async def test_login_rejects_bad_password(client):
    response = await client.post(
        "/auth/login",
        json={"email": "rick@saturn.ai", "password": "wrong"},
    )
    assert response.status_code == 401


async def test_protected_route_requires_auth():
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        response = await anon.get("/agents")
        assert response.status_code == 401


async def test_logout_revokes_session(client):
    me = await client.get("/auth/me")
    assert me.status_code == 200
    logout = await client.post("/auth/logout")
    assert logout.status_code == 204
    denied = await client.get("/auth/me")
    assert denied.status_code == 401

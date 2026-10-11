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

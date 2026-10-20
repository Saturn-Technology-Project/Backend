async def test_local_requests_pass_when_the_secret_is_unset(client, monkeypatch):
    monkeypatch.delenv("ORIGIN_AUTH_SECRET", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert (await client.get("/health")).status_code == 200


async def test_cloud_run_fails_closed_without_the_secret(client, monkeypatch):
    monkeypatch.delenv("ORIGIN_AUTH_SECRET", raising=False)
    monkeypatch.setenv("K_SERVICE", "saturn-backend")
    assert (await client.get("/health")).status_code == 403


async def test_wrong_origin_header_is_rejected(client, monkeypatch):
    monkeypatch.setenv("ORIGIN_AUTH_SECRET", "expected-value")
    response = await client.get("/health", headers={"X-Saturn-Origin-Auth": "other-value"})
    assert response.status_code == 403


async def test_matching_origin_header_is_accepted(client, monkeypatch):
    monkeypatch.setenv("ORIGIN_AUTH_SECRET", "expected-value")
    response = await client.get("/health", headers={"X-Saturn-Origin-Auth": "expected-value"})
    assert response.status_code == 200


async def test_healthz_does_not_require_the_origin_header(client, monkeypatch):
    monkeypatch.setenv("ORIGIN_AUTH_SECRET", "expected-value")
    assert (await client.get("/healthz")).status_code == 200


def test_previous_origin_secret_still_works_during_rotation(monkeypatch):
    from app.core.origin_auth import origin_allowed

    monkeypatch.setenv("K_SERVICE", "saturn-backend")
    monkeypatch.setenv("ORIGIN_AUTH_SECRET", "new-secret")
    monkeypatch.setenv("ORIGIN_AUTH_SECRET_PREVIOUS", "old-secret")
    assert origin_allowed("/health", {"X-Saturn-Origin-Auth": "new-secret"})
    assert origin_allowed("/health", {"X-Saturn-Origin-Auth": "old-secret"})
    assert not origin_allowed("/health", {"X-Saturn-Origin-Auth": "other"})
    assert not origin_allowed("/health", {"X-Saturn-Origin-Auth": ""})

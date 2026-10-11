import base64

import pytest

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


@pytest.mark.asyncio
async def test_personal_photo_and_org_photo_permissions(client):
    me = await client.patch("/auth/me/photo", json={"photo": PNG})
    assert me.status_code == 200, me.text
    assert me.json()["photo"] == PNG
    assert (await client.get("/auth/me")).json()["photo"] == PNG

    cleared = await client.patch("/auth/me/photo", json={"photo": None})
    assert cleared.status_code == 200
    assert cleared.json()["photo"] is None

    rejected = await client.patch("/auth/me/photo", json={"photo": "data:text/plain;base64,YQ=="})
    assert rejected.status_code == 400

    oversized = "data:image/png;base64," + base64.b64encode(b"\x00" * (512 * 1024 + 1)).decode()
    too_big = await client.patch("/auth/me/photo", json={"photo": oversized})
    assert too_big.status_code == 400

    org = await client.patch("/org/photo", json={"photo": PNG})
    assert org.status_code == 200, org.text
    assert org.json()["photo"] == PNG
    assert (await client.get("/org")).json()["organization"]["photo"] == PNG

    invited = await client.post(
        "/org/invites",
        json={"email": "photo-viewer@saturn.ai", "name": "Viewer", "title": "Analyst", "role": "viewer"},
    )
    assert invited.status_code == 201, invited.text
    accepted = await client.post(
        "/auth/accept-invite",
        json={"token": invited.json()["token"], "password": "password1"},
    )
    assert accepted.status_code == 200, accepted.text
    headers = {"Authorization": f"Bearer {accepted.json()['token']}"}

    own = await client.patch("/auth/me/photo", headers=headers, json={"photo": PNG})
    assert own.status_code == 200, own.text
    assert own.json()["photo"] == PNG
    assert own.json()["organization"]["photo"] == PNG

    denied = await client.patch("/org/photo", headers=headers, json={"photo": None})
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Missing team.write access"
    assert (await client.get("/org")).json()["organization"]["photo"] == PNG

    removed = await client.patch("/org/photo", json={"photo": None})
    assert removed.status_code == 200
    assert removed.json()["photo"] is None

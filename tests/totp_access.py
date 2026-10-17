from app.features.identity.auth.services.totp import current_code


async def finish_totp(client, headers: dict[str, str]) -> None:
    setup = await client.post("/auth/totp/setup", headers=headers)
    assert setup.status_code == 200, setup.text
    confirmed = await client.post(
        "/auth/totp/confirm",
        headers=headers,
        json={"code": current_code(setup.json()["secret"])},
    )
    assert confirmed.status_code == 204, confirmed.text

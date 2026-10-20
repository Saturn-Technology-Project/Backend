import time

from app.features.identity.auth.services.totp import current_code

# Secrets of accounts that set up TOTP in this test run, by email, so sign-in helpers can answer the prompt.
SECRETS: dict[str, str] = {}


async def finish_totp(client, headers: dict[str, str]) -> str:
    setup = await client.post("/auth/totp/setup", headers=headers)
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    confirmed = await client.post(
        "/auth/totp/confirm",
        headers=headers,
        json={"code": current_code(secret)},
    )
    assert confirmed.status_code == 204, confirmed.text
    me = await client.get("/auth/me", headers=headers)
    if me.status_code == 200:
        SECRETS[me.json()["email"]] = secret
    return secret


def next_code(secret: str) -> str:
    """A code for the next 30-second step. The current one was already used and is refused as a replay."""
    return current_code(secret, time.time() + 30)

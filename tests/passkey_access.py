"""A software passkey for tests: it signs WebAuthn sign-in answers the way a browser would."""

import base64
import hashlib
import json
import secrets
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

ORIGIN = "http://localhost:3000"


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class SoftPasskey:
    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = _b64u(secrets.token_bytes(16))
        self._count = 0

    def public_key(self) -> str:
        """The COSE public key, encoded the way the passkeys table stores it."""
        numbers = self._key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
        return _b64u(cbor2.dumps(cose))

    def answer(self, options: dict, origin: str = ORIGIN) -> dict:
        self._count += 1
        client = json.dumps(
            {"type": "webauthn.get", "challenge": options["challenge"], "origin": origin, "crossOrigin": False}
        ).encode()
        # Flags: user present and user verified.
        data = hashlib.sha256(options["rpId"].encode()).digest() + bytes([0x05]) + struct.pack(">I", self._count)
        signature = self._key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": self.credential_id,
            "rawId": self.credential_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64u(client),
                "authenticatorData": _b64u(data),
                "signature": _b64u(signature),
            },
            "clientExtensionResults": {},
        }


async def enrol(session, user_id: str) -> SoftPasskey:
    from app.core.ids import new_id
    from app.models import Passkey

    passkey = SoftPasskey()
    session.add(
        Passkey(
            id=new_id("pkey"),
            user_id=user_id,
            credential_id=passkey.credential_id,
            public_key=passkey.public_key(),
            sign_count=0,
        )
    )
    await session.flush()
    return passkey

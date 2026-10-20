"""Vendored third-party files must match the upstream release recorded in docs/vendored-assets.md."""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# hash-wasm 4.12.0, package/dist/argon2.umd.min.js from
# https://registry.npmjs.org/hash-wasm/-/hash-wasm-4.12.0.tgz
ARGON2_JS = ROOT / "app/features/identity/oauth/static/argon2.umd.min.js"
ARGON2_JS_SHA256 = "dcec617a2e1b700fa132d1583a186cb70611113395e869f2dd6cc82b415d3094"


def test_argon2_bundle_is_hash_wasm_4_12_0():
    digest = hashlib.sha256(ARGON2_JS.read_bytes()).hexdigest()
    assert digest == ARGON2_JS_SHA256, (
        "argon2.umd.min.js changed. If this is an intended hash-wasm upgrade, verify the new file "
        "against the npm tarball and update docs/vendored-assets.md and this test together."
    )


def test_argon2_bundle_version_is_documented():
    doc = (ROOT / "docs/vendored-assets.md").read_text(encoding="utf-8")
    assert ARGON2_JS_SHA256 in doc
    assert "hash-wasm" in doc and "4.12.0" in doc

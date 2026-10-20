# Vendored assets

Third-party files committed to this repo instead of installed by a package
manager. Each one is pinned by sha256, and `tests/test_vendored_assets.py`
fails if the file changes without this table changing too.

| File | Upstream | Version | sha256 |
|---|---|---|---|
| `app/features/identity/oauth/static/argon2.umd.min.js` | [hash-wasm](https://www.npmjs.com/package/hash-wasm), `package/dist/argon2.umd.min.js` | 4.12.0 | `dcec617a2e1b700fa132d1583a186cb70611113395e869f2dd6cc82b415d3094` |

`/oauth/argon2.js` serves the argon2 bundle. The OAuth sign-in page uses it to
hash the password in the browser before it is sent.

## Verifying or upgrading

```bash
VERSION=4.12.0
curl -sSfLO "https://registry.npmjs.org/hash-wasm/-/hash-wasm-${VERSION}.tgz"
# Compare with the "integrity" field of https://registry.npmjs.org/hash-wasm/${VERSION}
echo "sha512-$(openssl dgst -sha512 -binary hash-wasm-${VERSION}.tgz | base64 -w0)"
tar -xzf "hash-wasm-${VERSION}.tgz" package/dist/argon2.umd.min.js
sha256sum package/dist/argon2.umd.min.js
```

The 4.12.0 tarball integrity is
`sha512-+/2B2rYLb48I/evdOIhP+K/DD2ca2fgBjp6O+GBEnCDk2e4rpeXIK8GvIyRPjTezgmWn9gmKwkQjjx6BtqDHVQ==`.

To upgrade, copy the new `package/dist/argon2.umd.min.js` over the vendored
file, then update the version and sha256 in this table and in
`tests/test_vendored_assets.py` in the same commit.

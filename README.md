# Saturn Backend

Execution loop: **Run → Agent → Tool → Policy → Approval → Wallet/x402 → Execute → Audit**

The control plane starts **empty**. Bootstrap creates a platform admin only — no demo agents, tools, MCP servers, policies, or wallets.

## Layout

Code is grouped by feature, then by function. A function keeps its own `models/`, `services/`, `controllers/`, and `routes/`.

```
app/
  core/            settings, ids, enums
  db/              engine and seed
  models/          ORM registry (`from app.models import …`)
  schemas.py       shared request and response models
  shared/          cross-feature association tables
  features/
    identity/      auth, oauth
    organization/  org, members, subscription
    platform/      admin
    agent/         agents
    mcp/           servers, oauth, saturn
    policy/        policies
    execution/     runs, approvals, activity
    billing/       wallets, payments, x402
    tooling/       tools, tool sets
    credentials/   vault
```

`app/main.py` mounts each function router. HTTP handlers live in `controllers/`. `routes/` is the mount point.

## Run locally

This repo is the API. Console and admin are separate repos.

```bash
cp .env.example .env
# Fill in POSTGRES_PASSWORD and REDIS_PASSWORD (for example `openssl rand -hex 16`),
# SEED_USER_EMAIL and SEED_USER_PASSWORD.
docker compose up --build
```

Set `SEED_USER_EMAIL` and `SEED_USER_PASSWORD` in `.env` before the first boot. Those values create the platform admin. Do not commit them. Compose refuses to start until `POSTGRES_PASSWORD` and `REDIS_PASSWORD` are set.

- API: http://localhost:8000
- Docs: http://localhost:8000/docs
- Postgres: 127.0.0.1:5433 (user `saturn`, password `POSTGRES_PASSWORD`)
- Redis: 127.0.0.1:6379 (password `REDIS_PASSWORD`)

Postgres and Redis listen on 127.0.0.1 only. The container runs as an unprivileged user (uid 10001). It applies migrations on start, unless `RUN_MIGRATIONS=false`.

## Auth

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$SEED_USER_EMAIL\",\"password\":\"$SEED_USER_PASSWORD\"}" | python -c 'import sys,json; print(json.load(sys.stdin)["token"])')

curl -s localhost:8000/auth/me -H "Authorization: Bearer $TOKEN"
```

## Org members

Org is the tenant. Users belong to one org as employees, with per-org access:

- `agents` / `policies` / `approvals` / `team` → `none` | `read` | `write`
- Roles: `admin`, `operator`, `viewer`, or `custom`

```bash
curl -s localhost:8000/org -H "Authorization: Bearer $TOKEN"
curl -s localhost:8000/org/members -H "Authorization: Bearer $TOKEN"
curl -s -X POST localhost:8000/org/invites \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email":"ops@saturn.ai","name":"Ops","role":"operator"}'
```

The invite link is emailed to the invitee. The response includes the one-time `token` only in local development (`IN_PROCESS_RUNTIME=true` without `K_SERVICE`). The member sets a password with `POST /auth/accept-invite`. An org must keep at least one admin (`team.write`).

Accounts with an authenticator app must finish sign-in with `POST /auth/login/totp` when `/auth/login` returns `mfa_required` with `methods` containing `totp`.

## Platform admin

The bootstrap user is a platform admin (`is_platform_admin`). The Admin Dashboard uses these endpoints to onboard tenants and inspect the fleet:

```bash
curl -s localhost:8000/admin/stats -H "Authorization: Bearer $TOKEN"
curl -s localhost:8000/admin/orgs -H "Authorization: Bearer $TOKEN"
curl -s -X POST localhost:8000/admin/orgs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Acme","admin_email":"admin@acme.test","admin_name":"Acme Admin","admin_password":"password1"}'
curl -s localhost:8000/admin/users -H "Authorization: Bearer $TOKEN"
```

## Tests

Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements-dev.txt
pytest
```

Pytest loads a local catalog fixture (`tests/catalog.py`) so the execution-loop tests do not depend on production seed data.

## Dependencies

| File | Contents |
|---|---|
| `requirements.in` | direct production dependencies, pinned with `==` |
| `requirements-dev.in` | test-only additions (pytest, pytest-asyncio, aiosqlite) |
| `requirements.txt` | full production lock with hashes, installed in the image |
| `requirements-dev.txt` | production lock plus test tools, with hashes, installed in CI |

Everything installs with `--require-hashes`, so any package, direct or transitive, must match a hash in the lock. The production image never contains the test tools. To add or bump a package, edit the `.in` file and regenerate both locks with the `uv pip compile` commands at the top of `requirements.in`. Commit the `.in` and `.txt` files together.

Vendored third-party files are listed with their versions and hashes in [`docs/vendored-assets.md`](docs/vendored-assets.md).

## Configuration

Settings come from the environment (or `.env`); see `.env.example`. Besides the app settings in `app/core/config.py`, the container reads:

| Variable | Default | Effect |
|---|---|---|
| `RUN_MIGRATIONS` | `true` | `false` skips `alembic upgrade head` on container start (production runs it in a Cloud Run Job instead) |
| `MIGRATION_LOCK_WAIT` | `300s` | how long a migration waits for another runner's advisory lock |
| `LEGACY_PASSWORD_UNTIL` | empty | ISO date after which legacy (pre-argon2) passwords stop working |

uvicorn does not trust `X-Forwarded-*` from any peer but 127.0.0.1.

## Deploy

Cloud Run + Cloud SQL + Memorystore. GitHub Actions on `main`, through the `production` environment.

See [`infra/gcp/README.md`](infra/gcp/README.md) for setup, every deploy variable, and the hardening runbooks (runtime service account, migration job, Redis TLS, ingress).

## License

[FSL-1.1-ALv2](LICENSE.md). Copyright Rick Weng. You may read, audit, modify and self-host this code for any purpose except offering a competing commercial service. Two years after each version is released, it also becomes available under Apache 2.0.

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
docker compose up --build
```

Set `SEED_USER_EMAIL` and `SEED_USER_PASSWORD` in `.env` before the first boot. Those values create the platform admin. Do not commit them.

- API: http://localhost:8000
- Docs: http://localhost:8000/docs
- Postgres: localhost:5433
- Redis: localhost:6379

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

The invite response includes a one-time `token`. The member sets a password with `POST /auth/accept-invite`. An org must keep at least one admin (`team.write`).

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
pip install -r requirements.txt
pytest
```

Pytest loads a local catalog fixture (`tests/catalog.py`) so the execution-loop tests do not depend on production seed data.

## Deploy

Cloud Run + Cloud SQL + Memorystore. GitHub Actions on `main`.

See [`infra/gcp/README.md`](infra/gcp/README.md).

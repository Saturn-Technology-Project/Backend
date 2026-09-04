# Saturn MVP Backend v0.1

Execution loop: **Run → Agent → Tool → Policy → Approval → Wallet/x402 → Execute → Audit**

The control plane starts **empty**. Bootstrap creates a platform admin only — no demo agents, tools, MCP servers, policies, or wallets.

## Run locally

Full stack (API + console + admin + Postgres + Redis) from the repo root:

```bash
docker compose up --build
```

- Console: http://localhost:3000
- Admin: http://localhost:3001
- API: http://localhost:8000
- Docs: http://localhost:8000/docs
- Postgres: localhost:5433 (avoids other local stacks on 5432)
- Redis: localhost:6379

API-only from this directory:

```bash
docker compose up --build
```

The API container still talks to Postgres on the internal Docker network.

## Auth

Set `SEED_USER_EMAIL` and `SEED_USER_PASSWORD` in `.env` (see `.env.example`). Those values bootstrap the first platform admin. Do not commit them.

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

## Agent WebSocket

Agents and MCP servers reach Saturn through **Saturn Connector**. The Connector holds one outbound WSS to `/ws/connectors`, then registers runtimes (`runtime.register`) and MCP servers (`mcp.register`). The Panel issues commands (`ping`, `run`) to a specific agent; Saturn multiplexes them onto that Connector socket.

First visit to Agents / MCP: enroll the Connector, then wait for it to register catalog.

```bash
curl -s -X POST localhost:8000/connectors/enroll \
  -H "Authorization: Bearer $TOKEN"
```

Response: `conn_…` token plus `ws_url`. Saturn stores the current token. `GET /connectors/enrollment` returns it until you rotate. Point the Connector at that WebSocket:

```
SATURN_CLOUD_WS=ws://localhost:8000/ws/connectors
CONNECTOR_TOKEN=conn_...
CONNECTOR_ID=conn_...
```

Or enroll a running Connector (`CONNECTOR_URL` is the Connector HTTP API on the host where it runs):

```bash
curl -s -X PUT "$CONNECTOR_URL/enrollment" \
  -H "Content-Type: application/json" \
  -d '{"token":"conn_...","cloudWs":"ws://host.docker.internal:8000/ws/connectors"}'
```

```
Saturn Connector  --WSS /ws/connectors?connector_id=&token=-->  Backend hub
  Agent runtimes  --WS  /ws  (local)------------------------->  Connector
  MCP servers     --HTTP /internal/mcp/register-------------->  Connector
Panel / console   --WS /ws/panel?token=sat_…----------------->  Backend hub
```

Panel commands (routed through the Connector to one agent):

```json
{"type":"command","command":"ping","agent_id":"<agent_id>","id":"cmd_1"}
{"type":"command","command":"run","agent_id":"<agent_id>","input":"Your task","id":"cmd_2"}
```

Per-agent `/ws/agents` enrollment still works for a single runtime without a Connector.

## Platform admin

The bootstrap user is a platform admin (`is_platform_admin`). The Admin Dashboard uses these endpoints to onboard tenants and inspect the whole fleet:

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

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

Pytest loads a local catalog fixture (`tests/catalog.py`) so the execution-loop tests do not depend on production seed data.

## Deploy

Cloud Run + Cloud SQL + Memorystore. GitHub Actions on `main`.

See [`infra/gcp/README.md`](infra/gcp/README.md).

# Deploy Saturn to Google Cloud

Each Saturn app is a separate GitHub repo. Push `main` to deploy that service to Cloud Run.

- Backend: Cloud Run (API `saturn-backend`, internal webhook worker `saturn-worker`) + Cloud SQL + Memorystore Redis
- Frontend: Cloud Run
- Dashboard: Cloud Run

GitHub authenticates with Workload Identity Federation. No JSON key is stored in the repo.

Every hardening option below is opt-in through a GitHub variable. When a variable is unset, the
workflow behaves as it did before the variable existed. The [runbooks](#runbooks) list the manual
GCP / GitHub step each option needs, in the order to apply them.

## One-time Google Cloud setup (new project)

This is the setup for a new project. An existing project should follow
[Split and narrow the deploy identities](#split-and-narrow-the-deploy-identities) instead.

### Workload Identity Federation

Only workflows running on `main` of a repo in your org can get a Google token. Pull requests and
other branches never can, even if a workflow asks for `id-token: write`.

```bash
PROJECT_ID=your-gcp-project
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
GITHUB_ORG=YOUR_GITHUB_ORG
POOL=github
PROVIDER=saturn

gcloud iam workload-identity-pools create "$POOL" \
  --location global \
  --display-name "GitHub"

gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
  --location global \
  --workload-identity-pool "$POOL" \
  --display-name "GitHub OIDC" \
  --issuer-uri "https://token.actions.githubusercontent.com" \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.owner=assertion.repository_owner,attribute.ref=assertion.ref" \
  --attribute-condition "assertion.repository_owner == '${GITHUB_ORG}' && assertion.ref == 'refs/heads/main'"
```

Jobs that use `environment: production` still carry `ref: refs/heads/main` in their token, so this
condition holds for the deploy and provision jobs.

Provider resource name to put in the `GCP_WORKLOAD_IDENTITY_PROVIDER` secret:

```
projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/saturn
```

### Identities

Use one identity per job, each bound to one repo only:

| Identity | Used by | Roles |
|---|---|---|
| `github-deployer-backend` | backend `deploy-cloud-run.yml` | `roles/run.developer` (project), `roles/run.admin` on the `saturn-backend` and `saturn-worker` services (lets the deploy set their invoker policy), `roles/vpcaccess.user` (project), `roles/artifactregistry.writer` on the `saturn` repository, `roles/iam.serviceAccountUser` on `saturn-runtime` only |
| `github-provisioner` | backend `provision-gcp.yml` | `roles/cloudsql.admin`, `roles/redis.admin`, `roles/compute.networkAdmin`, `roles/vpcaccess.admin`, `roles/artifactregistry.admin`, `roles/serviceusage.serviceUsageAdmin` |
| `saturn-runtime` | the API, the worker and the migration job at runtime | see [Runtime service account](#runtime-service-account) |
| one deployer per other repo | `saturn-frontend`, `saturn-dashboard`, `acme-demo` (repo `demosystem`) | `roles/run.developer` plus `roles/run.admin` on its own service, `roles/artifactregistry.writer` on its registry, `roles/iam.serviceAccountUser` on its own runtime SA |

No identity holds `roles/iam.serviceAccountUser` on the whole project. That role on the project
lets the holder deploy a service that runs as *any* service account. The frontend repos cannot
redeploy the backend.

```bash
REGION=asia-east1
GAR_LOCATION=asia-east1
DEPLOYER=github-deployer-backend@${PROJECT_ID}.iam.gserviceaccount.com
PROVISIONER=github-provisioner@${PROJECT_ID}.iam.gserviceaccount.com
REPO_PRINCIPAL="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_ORG}/saturn-backend"

gcloud iam service-accounts create github-deployer-backend --display-name "GitHub deployer (backend)"
gcloud iam service-accounts create github-provisioner --display-name "GitHub provisioner (backend)"

for SA in "$DEPLOYER" "$PROVISIONER"; do
  gcloud iam service-accounts add-iam-policy-binding "$SA" \
    --role roles/iam.workloadIdentityUser \
    --member "$REPO_PRINCIPAL"
done

for ROLE in roles/run.developer roles/vpcaccess.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:${DEPLOYER}" --role "$ROLE"
done

for ROLE in roles/cloudsql.admin roles/redis.admin roles/compute.networkAdmin \
  roles/vpcaccess.admin roles/artifactregistry.admin roles/serviceusage.serviceUsageAdmin; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:${PROVISIONER}" --role "$ROLE"
done
```

Set the backend repo secrets: `GCP_SERVICE_ACCOUNT_EMAIL=$DEPLOYER` and
`GCP_PROVISION_SERVICE_ACCOUNT_EMAIL=$PROVISIONER`. Then run **Provision Google Cloud Resources**.
Then grant the resource-level roles, which need the resources to exist first:

```bash
gcloud artifacts repositories add-iam-policy-binding saturn \
  --location "$GAR_LOCATION" --member "serviceAccount:${DEPLOYER}" --role roles/artifactregistry.writer
```

Create the runtime service account ([below](#runtime-service-account)) with
`DEPLOYER_SA=$DEPLOYER`, and set `RUNTIME_SERVICE_ACCOUNT`. After the first deploy has created the
service, run:

```bash
gcloud run services add-iam-policy-binding saturn-backend \
  --region "$REGION" --member "serviceAccount:${DEPLOYER}" --role roles/run.admin
```

The first deploy of a new project needs `roles/run.admin` on the project for one run, because the
service does not exist yet. Grant it, deploy once, then remove it.

### GitHub environment and branch protection

The deploy and provision jobs run in the `production` environment. GitHub creates the environment
on first use, with no rules. Add the rules under **Settings → Environments → production**:

- **Required reviewers**: at least one person other than the author.
- **Deployment branches and tags**: *Selected branches and tags*, rule `main`.

Under **Settings → Branches** (or Rulesets), protect `main`:

- Require a pull request before merging, with at least one approval.
- Require the `Test / test` status check.
- Block force pushes and deletion. Do not let administrators bypass the rules.

The deploy job also refuses to run from any ref but `refs/heads/main`, so a manual **Run workflow**
from another branch fails before it authenticates.

## Provision data plane

In the backend GitHub repo, set the variables below, then run **Actions → Provision Google Cloud Resources**.

| Variable | Example |
|---|---|
| `GCP_PROJECT_ID` | `saturn-prod` |
| `GCP_REGION` | `asia-east1` |
| `GAR_LOCATION` | `asia-east1` |
| `GAR_REPOSITORY` | `saturn` |
| `CLOUD_SQL_INSTANCE` | `saturn-pg` |
| `CLOUD_SQL_DATABASE` | `saturn` |
| `CLOUD_SQL_USER` | `saturn` |
| `CLOUD_SQL_TIER` | `db-g1-small` (Enterprise edition) |
| `CLOUD_SQL_AVAILABILITY_TYPE` | `zonal` (default) or `regional` |
| `CLOUD_SQL_BACKUP_START_TIME` | `18:00` (UTC, default) |
| `REDIS_INSTANCE` | `saturn-redis` |
| `REDIS_TIER` | `BASIC` |
| `REDIS_SIZE_GB` | `1` |
| `VPC_NETWORK` | `default` |
| `SERVERLESS_VPC_CONNECTOR` | `saturn-vpc` |
| `SERVERLESS_VPC_CONNECTOR_RANGE` | `10.8.0.0/28` |
| `SETUP_RUNTIME_SERVICE_ACCOUNT` | unset; `true` only if the provision identity may grant IAM |

Secret: `CLOUD_SQL_PASSWORD`.

`bootstrap.sh` only creates what is missing; it never modifies an existing instance. On an existing
instance it prints a warning for each hardening setting that is off. New resources are created
hardened:

- **Cloud SQL**: requires TLS (`--ssl-mode ENCRYPTED_ONLY`; Cloud Run's Cloud SQL connection already
  uses TLS), deletion protection, daily backups kept 14 days, point-in-time recovery with 7 days of
  logs, private IP only. The database user is created through the Cloud SQL Admin API. The password
  goes in a request body on stdin, never in a command-line argument.
- **Redis**: AUTH and in-transit encryption (TLS on port 6378, server authentication).

The job prints the Cloud SQL connection name and the Redis host and port. Then create the secrets:

```
DATABASE_URL=postgresql+asyncpg://saturn:PASSWORD@/saturn?host=/cloudsql/PROJECT:REGION:INSTANCE
CLOUD_SQL_INSTANCE_CONNECTION_NAME=PROJECT:REGION:INSTANCE   # GitHub variable
```

For a new (TLS) Redis, create `REDIS_URL` and `REDIS_CA` as in
[Move Redis to AUTH and TLS](#move-redis-to-auth-and-tls) (steps 2 and 3), and set the
`REDIS_CA_SECRET` variable. An instance without TLS uses `REDIS_URL=redis://REDIS_HOST:6379/0`.

## GitHub variables and secrets

Shared by all three repos:

| Name | Type | Used by |
|---|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | secret | all |
| `GCP_SERVICE_ACCOUNT_EMAIL` | secret | all (each repo its own deployer) |
| `GCP_PROJECT_ID` | variable | all |
| `GCP_REGION` | variable | all |
| `GAR_LOCATION` | variable | all |
| `GAR_REPOSITORY` | variable | all |
| `BACKEND_PUBLIC_URL` | variable | frontend, dashboard, backend (`PUBLIC_WS_URL`) |

Backend only:

| Name | Type | Default when unset |
|---|---|---|
| `BACKEND_CLOUD_RUN_SERVICE` | variable, e.g. `saturn-backend` | `saturn-backend` |
| `CLOUD_SQL_INSTANCE_CONNECTION_NAME` | variable | required |
| `SERVERLESS_VPC_CONNECTOR` | variable | required |
| `SATURN_KMS_KEY` | variable, full `cryptoKeyVersions/N` name of the MAC key | required |
| `GCP_PROVISION_SERVICE_ACCOUNT_EMAIL` | secret | provisioning uses `GCP_SERVICE_ACCOUNT_EMAIL` |
| `RUNTIME_SERVICE_ACCOUNT` | variable, e.g. `saturn-runtime@PROJECT.iam.gserviceaccount.com` | services keep their current identity (the default compute SA) |
| `CLOUD_RUN_INGRESS` | variable: `all`, `internal`, `internal-and-cloud-load-balancing` | the API keeps its current ingress |
| `MIGRATIONS_JOB` | variable, `true` to run migrations in a Cloud Run Job | each container migrates on start |
| `MIGRATIONS_JOB_NAME` | variable | `saturn-migrate` |
| `MIGRATIONS_DATABASE_URL_SECRET` | variable, secret name holding a schema-owner DSN | `DATABASE_URL` |
| `MIGRATIONS_SERVICE_ACCOUNT` | variable | `RUNTIME_SERVICE_ACCOUNT` |
| `SEED_ON_DEPLOY` | variable, `true` to mount `SEED_USER_*` on the API | not mounted |
| `REDIS_URL_SECRET` | variable, secret name holding `REDIS_URL` | `REDIS_URL` |
| `REDIS_CA_SECRET` | variable, secret name holding the Memorystore server CA (PEM) | no CA mounted |
| `LEGACY_PASSWORD_UNTIL` | variable, `YYYY-MM-DD` | legacy passwords accepted with no end date |
| `IMAGE_ATTESTATIONS` | variable, `true` for provenance and SBOM | no attestations |
| `MAX_INSTANCES` | variable | `10` |
| `DATABASE_URL`, `REDIS_URL` | Secret Manager | required |
| `SEED_USER_EMAIL`, `SEED_USER_PASSWORD` | Secret Manager | only read when `SEED_ON_DEPLOY=true` |
| `ORIGIN_AUTH_SECRET` | Secret Manager | required on Cloud Run |
| `CONSOLE_PROXY_SECRET_NAME` | variable, secret name shared with the console (set the same variable on Frontend) | console users share the per-address login limits |
| `CORS_ORIGINS` | variable, optional, comma-separated browser origins if the API is called cross-origin | |

Frontend / Dashboard:

| Name | Type |
|---|---|
| `FRONTEND_CLOUD_RUN_SERVICE` | variable, e.g. `saturn-frontend` |
| `DASHBOARD_CLOUD_RUN_SERVICE` | variable, e.g. `saturn-dashboard` |

`BACKEND_PUBLIC_URL` is the public API origin, for example `https://api.saturn-core.com`. Frontend and Dashboard bake this into the `/saturn-api` HTTP rewrite. Backend sets `PUBLIC_WS_URL` to that origin so MCP OAuth callbacks use `${PUBLIC_WS_URL}/mcp-servers/oauth/callback`.

### Secrets per service

`--set-secrets` replaces the whole set on every deploy, so a secret left out of the list is
unmounted.

| Secret | API | Worker | Migration job |
|---|---|---|---|
| `DATABASE_URL` | yes | yes | yes (or `MIGRATIONS_DATABASE_URL_SECRET`) |
| `REDIS_URL` (or `REDIS_URL_SECRET`), `REDIS_CA_SECRET` | yes | yes | no |
| `RESEND_API_KEY`, `RESEND_FROM` | yes | yes (approval expiry sends the task-outcome email) | no |
| `SATURN_GITHUB_OAUTH_*`, `SATURN_GOOGLE_OAUTH_*` | yes | no | no |
| `POLICY_LLM_*` | yes | no | no |
| `ORIGIN_AUTH_SECRET` | yes | no (internal ingress; fails closed) | no |
| `CONSOLE_PROXY_SECRET_NAME` (when set) | yes | no | no |
| `SEED_USER_EMAIL`, `SEED_USER_PASSWORD` | only with `SEED_ON_DEPLOY=true` | no | no |

## Deploy

Frontend and backend go to Cloud Run only through GitHub Actions. Do not deploy them with a local `docker push` or `gcloud run` unless that path is explicitly requested.

1. Run **Provision Google Cloud Resources** once from the backend repo.
2. Push `main` on backend. Actions tests the commit, waits for the `production` environment's reviewers, builds `linux/amd64` and deploys that image digest. If `MIGRATIONS_JOB=true`, it runs `alembic upgrade head` once in the migration job before deploying either service. Environment variables come from this repo's GitHub secrets and variables, and that file replaces the Cloud Run environment.
3. Copy the Cloud Run URL into `BACKEND_PUBLIC_URL` when that URL changes.
4. Push `main` on frontend, then dashboard. Deploy backend before frontend, because the frontend bakes `BACKEND_PUBLIC_URL` into the image.

Each deploy workflow also has **Run workflow** for a manual release. It deploys only when run on `main`.

The container runs as uid 10001. uvicorn no longer gets `--proxy-headers --forwarded-allow-ips='*'`,
so it trusts `X-Forwarded-*` only from 127.0.0.1. Behind Cloud Run, `request.client` is the Google
front end and `request.url.scheme` is `http`. Code that needs the public origin or scheme must use
`PUBLIC_WS_URL` (`app/core/public_origin.py`), not `request.base_url`.

## Runbooks

Apply them in this order. Each step is safe on its own, and nothing changes until its variable is
set.

### Runtime service account

Without `RUNTIME_SERVICE_ACCOUNT`, both services run as the project's default compute service
account. In most projects that account holds `roles/editor`, so any SSRF or RCE in the app can read
every secret and use every KMS key through the metadata server.

`runtime-service-account.sh` creates `saturn-runtime` with:

- `roles/secretmanager.secretAccessor` on each runtime secret by name, not project-wide
- `roles/cloudkms.signerVerifier` on the one key named by `SATURN_KMS_KEY`, the MAC key the app derives tenant keys with (`pq_crypto.py`)
- `roles/cloudsql.client`, `roles/logging.logWriter`, `roles/monitoring.metricWriter`

Redis needs no IAM: access is by network plus AUTH.

1. As a project IAM admin:

   ```bash
   GCP_PROJECT_ID=saturn-prod \
   SATURN_KMS_KEY=projects/saturn-prod/locations/asia-east1/keyRings/saturn/cryptoKeys/kem-root/cryptoKeyVersions/1 \
   DEPLOYER_SA=github-deployer-backend@saturn-prod.iam.gserviceaccount.com \
   bash infra/gcp/runtime-service-account.sh   # from the Backend repo root
   ```

   Add `RUNTIME_SECRETS="... REDIS_URL_TLS REDIS_CA"` (the defaults plus any extra names) when you
   use `REDIS_URL_SECRET` / `REDIS_CA_SECRET` or `MIGRATIONS_DATABASE_URL_SECRET`. Add
   `SEED_USER_EMAIL SEED_USER_PASSWORD` only while `SEED_ON_DEPLOY=true`.
2. Set the GitHub variable `RUNTIME_SERVICE_ACCOUNT` to the printed email and deploy. The migration
   job uses the same account unless `MIGRATIONS_SERVICE_ACCOUNT` is set.
3. Check both services serve and the worker delivers webhooks, then remove project-wide access from
   the default compute account if nothing else uses it (for example `roles/editor`).

Cloud Run keeps the last service account it was given. Unsetting the variable later does not switch
back; deploy once with `--service-account` set to the old account to revert.

If `SATURN_SECRET_PROJECT` is ever turned on, the app also creates and deletes `sat-org-*` secrets.
Grant `roles/secretmanager.admin` with an IAM condition
`resource.name.startsWith("projects/PROJECT_NUMBER/secrets/sat-")` in that project. Do not grant it
project-wide.

### Split and narrow the deploy identities

`split-deploy-identities.sh` does steps 1-4 (it only adds accounts and bindings, and prints the
GitHub commands), and `retire-shared-deployer.sh` does step 5 and [Bind tokens to main](#bind-tokens-to-main).
The second script refuses to run while any service still runs as the default compute account or
was last deployed by the shared deployer. It also takes the default compute account's access to
runtime secrets, the KMS key and Cloud SQL.

Before the split, one `github-deployer` was shared by every repo. It holds project-wide `run.admin`,
`iam.serviceAccountUser`, `cloudsql.admin`, `redis.admin`, `compute.networkAdmin` and
`vpcaccess.admin`, so a compromised frontend CI can redeploy the backend with any identity. Move to
the identities in [Identities](#identities):

1. Create `github-deployer-backend` and `github-provisioner`, bind each to
   `attribute.repository/ORG/saturn-backend` only, and grant the roles in the table.
2. Run [Runtime service account](#runtime-service-account) with
   `DEPLOYER_SA=github-deployer-backend@...`, so the new deployer can act as `saturn-runtime`.
3. Set the backend secrets `GCP_SERVICE_ACCOUNT_EMAIL` (new deployer) and
   `GCP_PROVISION_SERVICE_ACCOUNT_EMAIL` (provisioner). Set `RUNTIME_SERVICE_ACCOUNT`. Deploy.
4. Give the frontend and dashboard repos their own deployers the same way, then remove their
   `workloadIdentityUser` bindings on the old `github-deployer`.
5. Remove every project-level role from the old `github-deployer`, then delete it:

   ```bash
   OLD=github-deployer@${PROJECT_ID}.iam.gserviceaccount.com
   for ROLE in roles/run.admin roles/artifactregistry.writer roles/cloudsql.admin roles/redis.admin \
     roles/compute.networkAdmin roles/vpcaccess.admin roles/iam.serviceAccountUser; do
     gcloud projects remove-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:${OLD}" --role "$ROLE"
   done
   ```

Do step 5 only after `RUNTIME_SERVICE_ACCOUNT` is set. Without project-wide `serviceAccountUser`, a
deploy that runs as the default compute account fails.

### Bind tokens to main

Restrict the existing provider so only `main` can get a token. This affects all three repos, and
they all deploy from `main`.

```bash
gcloud iam workload-identity-pools providers update-oidc saturn \
  --location global \
  --workload-identity-pool github \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.owner=assertion.repository_owner,attribute.ref=assertion.ref" \
  --attribute-condition "assertion.repository_owner == 'YOUR_GITHUB_ORG' && assertion.ref == 'refs/heads/main'"
```

Also set the `production` environment rules and branch protection
([above](#github-environment-and-branch-protection)). For extra safety against a renamed or
recreated org or repo, match on `assertion.repository_owner_id` and `assertion.repository_id`
instead of the names.

### Migrations in a Cloud Run Job

Without the job, every container start runs `alembic upgrade head` with the app's DB user.
`alembic/env.py` now serialises concurrent runners with a Postgres advisory lock. Other backends are
no longer terminated: a DDL statement that waits more than 60 s on a lock held by live traffic fails
the migration, and it can be retried.

1. Make sure the deployer can create and run jobs (`roles/run.developer`, which `run.admin` also
   covers) and can act as the job's service account. The job's account needs `secretAccessor` on
   `DATABASE_URL` (or `MIGRATIONS_DATABASE_URL_SECRET`) and `roles/cloudsql.client`. The runtime
   service account already has both.
2. Set `MIGRATIONS_JOB=true` and deploy. The workflow deploys and executes
   `saturn-migrate` (`alembic upgrade head`, one task, no retries, 15 min timeout) and waits for it.
   It then deploys both services with `RUN_MIGRATIONS=false`. A failed migration stops the release
   before any service changes.
3. Optional, least-privilege database: create a schema-owner role for migrations and a DML-only role
   for the app. Put the owner DSN in a secret, set `MIGRATIONS_DATABASE_URL_SECRET` to its name, and
   point `DATABASE_URL` at the DML-only role.
4. Optional: `ALTER ROLE saturn SET idle_in_transaction_session_timeout = '60s';`. This bounds the
   stuck transactions that `env.py` used to terminate.

### Move Redis to AUTH and TLS

Memorystore cannot turn on transit encryption for an existing instance, so the move is to a new
instance. Redis holds rate-limit counters and MCP session state only. Both start empty on the new
instance: MCP clients reconnect, and rate-limit windows restart.

```bash
REGION=asia-east1
NEW=saturn-redis-tls
```

`redis-tls.sh` does steps 1-4 and 6.

1. Create the instance:

   ```bash
   gcloud redis instances create "$NEW" --region "$REGION" --redis-version redis_7_0 \
     --tier BASIC --size 1 --network "projects/${PROJECT_ID}/global/networks/default" \
     --connect-mode private-service-access --enable-auth --transit-encryption-mode server-authentication
   ```

2. Store the server CA. Memorystore lists more than one certificate while it rotates its CA; keep
   them all.

   ```bash
   gcloud redis instances describe "$NEW" --region "$REGION" --format=json |
     python3 -c 'import json, sys; print("\n".join(c["cert"] for c in json.load(sys.stdin)["serverCaCerts"]))' |
     gcloud secrets create REDIS_CA --replication-policy automatic --data-file -
   ```

3. Store the URL without printing the AUTH string. Cloud Run mounts the CA at
   `/etc/secrets/redis-ca/ca.pem`. redis-py reads `ssl_ca_certs` and `ssl_cert_reqs` from the URL, so
   the app needs no change.

   ```bash
   HOST=$(gcloud redis instances describe "$NEW" --region "$REGION" --format='value(host)')
   PORT=$(gcloud redis instances describe "$NEW" --region "$REGION" --format='value(port)')
   AUTH=$(gcloud redis instances get-auth-string "$NEW" --region "$REGION" --format='value(authString)')
   printf 'rediss://:%s@%s:%s/0?ssl_cert_reqs=required&ssl_ca_certs=/etc/secrets/redis-ca/ca.pem' \
     "$AUTH" "$HOST" "$PORT" | gcloud secrets create REDIS_URL_TLS --replication-policy automatic --data-file -
   unset AUTH
   ```

4. Grant the runtime identity `secretAccessor` on `REDIS_URL_TLS` and `REDIS_CA`. Re-run
   `runtime-service-account.sh` with them in `RUNTIME_SECRETS`, or bind them to whichever account
   the services run as.
5. Optional dry run: a one-off job that pings the new instance with the same image and mounts:

   ```bash
   gcloud run jobs create redis-check --region "$REGION" --image IMAGE@DIGEST \
     --command python --args=-c,"import os;import redis;print(redis.Redis.from_url(os.environ['REDIS_URL']).ping())" \
     --set-secrets REDIS_URL=REDIS_URL_TLS:latest,/etc/secrets/redis-ca/ca.pem=REDIS_CA:latest \
     --vpc-connector saturn-vpc --vpc-egress private-ranges-only --execute-now --wait
   gcloud run jobs delete redis-check --region "$REGION" --quiet
   ```

6. Set the GitHub variables `REDIS_URL_SECRET=REDIS_URL_TLS` and `REDIS_CA_SECRET=REDIS_CA`, then
   deploy. Login and other rate-limited endpoints fail closed if Redis is unreachable, so watch the
   first minutes.
7. When traffic is healthy, delete the old instance and the old `REDIS_URL` secret.

When Memorystore announces a server CA rotation, repeat step 2 as a new version of `REDIS_CA`
(`gcloud secrets versions add`) and redeploy. redis-py 5 verifies the chain but not the hostname.
If a later redis-py checks hostnames by default and the Memorystore certificate does not name the
IP, add `&ssl_check_hostname=false` to the URL. The CA check alone still blocks a spoofed server.

### Harden an existing Cloud SQL instance

`bootstrap.sh` warns but never changes an existing instance. In a maintenance window:

```bash
INSTANCE=saturn-pg
gcloud sql instances patch "$INSTANCE" --deletion-protection
gcloud sql instances patch "$INSTANCE" --backup-start-time 18:00 --retained-backups-count 14 \
  --enable-point-in-time-recovery --retained-transaction-log-days 7
# Cloud Run's Cloud SQL connection already uses TLS. Check first that no other client
# (a VM, or psql over private IP) connects without TLS.
gcloud sql instances patch "$INSTANCE" --ssl-mode ENCRYPTED_ONLY
```

For high availability, also run `--availability-type regional` (this roughly doubles the cost).

Earlier versions of `bootstrap.sh` passed `CLOUD_SQL_PASSWORD` as a command-line argument on the CI
runner. `rotate-db-password.sh` rotates it end to end: a new `DATABASE_URL` version, the password set
through the Admin API, the API and worker rolled onto the new version, and the GitHub secret. By
hand, use
`gcloud sql users set-password saturn --instance saturn-pg --prompt-for-password` at an interactive
terminal. Then update the `DATABASE_URL` secret and redeploy.

### Restrict ingress to Cloudflare

The API currently accepts traffic on its `*.run.app` URL from anywhere. The only thing stopping a
request that bypasses Cloudflare (WAF, bot rules, rate rules) is the static `ORIGIN_AUTH_SECRET`
header. The target setup is Cloudflare → Google external Application Load Balancer, with Cloud Armor
allowing only Cloudflare IPs → Cloud Run with ingress `internal-and-cloud-load-balancing`.

`cloudflare-ingress.sh` does steps 1 and 2 from the Origin CA certificate files.

1. Load balancer in front of the service. Use a Cloudflare Origin CA certificate: a Google-managed
   certificate cannot validate while Cloudflare proxies the hostname.

   ```bash
   gcloud compute network-endpoint-groups create saturn-backend-neg --region "$REGION" \
     --network-endpoint-type serverless --cloud-run-service saturn-backend
   gcloud compute backend-services create saturn-backend-bs --global --load-balancing-scheme EXTERNAL_MANAGED
   gcloud compute backend-services add-backend saturn-backend-bs --global \
     --network-endpoint-group saturn-backend-neg --network-endpoint-group-region "$REGION"
   gcloud compute url-maps create saturn-backend-lb --default-service saturn-backend-bs
   gcloud compute ssl-certificates create saturn-api-origin --global \
     --certificate origin.pem --private-key origin-key.pem
   gcloud compute target-https-proxies create saturn-backend-https \
     --url-map saturn-backend-lb --ssl-certificates saturn-api-origin
   gcloud compute addresses create saturn-backend-ip --global
   gcloud compute forwarding-rules create saturn-backend-https --global \
     --load-balancing-scheme EXTERNAL_MANAGED --address saturn-backend-ip \
     --target-https-proxy saturn-backend-https --ports 443
   ```

2. Cloud Armor: deny by default, and allow Cloudflare's published ranges, at most 10 per rule.

   ```bash
   gcloud compute security-policies create saturn-cloudflare-only
   gcloud compute security-policies rules update 2147483647 --security-policy saturn-cloudflare-only --action deny-403
   priority=1000
   { curl -sSf https://www.cloudflare.com/ips-v4; echo; curl -sSf https://www.cloudflare.com/ips-v6; } |
     grep . | xargs -n 10 | while read -r ranges; do
       gcloud compute security-policies rules create "$priority" --security-policy saturn-cloudflare-only \
         --src-ip-ranges "${ranges// /,}" --action allow
       priority=$((priority + 1))
     done
   gcloud compute backend-services update saturn-backend-bs --global --security-policy saturn-cloudflare-only
   ```

   Cloudflare's ranges change rarely, but they do change. Re-run this when
   https://www.cloudflare.com/ips/ changes, or schedule it.
3. Point the Cloudflare DNS record for `api.saturn-core.com` at the load balancer IP (proxied), with
   SSL mode **Full (strict)**. Optionally turn on Authenticated Origin Pulls and require the
   Cloudflare client certificate on the load balancer (mTLS).
4. Set `CLOUD_RUN_INGRESS=internal-and-cloud-load-balancing` and deploy. The `*.run.app` URL then
   refuses outside traffic. To roll back, set the variable to `all` and deploy.
5. Rotate `ORIGIN_AUTH_SECRET`, since the old value may have been exposed while the origin was
   public. The app accepts one value at a time, so a rotation has a short window of 403s:
   1. `openssl rand -hex 32 | gcloud secrets versions add ORIGIN_AUTH_SECRET --data-file -`
   2. Update the Cloudflare Transform Rule that sets `X-Saturn-Origin-Auth` to the new value.
   3. Immediately deploy the backend (or **Run workflow**), so the service picks up `:latest`.

   Rotate again on any suspected leak, and at least yearly.

Once only Cloudflare can reach the service, the `CF-Connecting-IP` header the app reads for rate
limits comes only from Cloudflare.

The console (Frontend) needs the same treatment: its `*.run.app` URL proxies `/saturn-api` to the
API. Put it behind its own load balancer and Cloud Armor policy the same way, then set
`CLOUD_RUN_INGRESS=internal-and-cloud-load-balancing` on the Frontend repo.

### Per-browser rate limits through the console

Console requests reach the API from the console's own address, so `CF-Connecting-IP` names the
console and every console user would share one login, sign-up and invite bucket. The console sends
each browser's address in `X-Saturn-Client-IP` with a secret only it and the API hold, and the API
trusts that address only when the secret matches.

1. `openssl rand -hex 32 | gcloud secrets create CONSOLE_PROXY_SECRET --data-file -`
2. Grant both runtime service accounts `roles/secretmanager.secretAccessor` on it.
3. Set the repository variable `CONSOLE_PROXY_SECRET_NAME=CONSOLE_PROXY_SECRET` on both Backend and
   Frontend, deploy the backend, then the frontend.

To rotate, set the old value as `CONSOLE_PROXY_SECRET_PREVIOUS` on the API while both services move to
the new version.

### Seeding the first admin

`SEED_USER_EMAIL` / `SEED_USER_PASSWORD` are mounted on the API only when `SEED_ON_DEPLOY=true`.
For a new project, set it for the first deploy and sign in. Then unset it and deploy again, and
disable each secret version (`gcloud secrets versions list SEED_USER_PASSWORD`, then
`gcloud secrets versions disable VERSION --secret SEED_USER_PASSWORD`, and the same for
`SEED_USER_EMAIL`).
An existing project already has its admin and needs nothing.

### Legacy passwords

Set `LEGACY_PASSWORD_UNTIL=YYYY-MM-DD` to stop accepting legacy (pre-argon2) passwords after that
day. Users who have not signed in by then must reset their password. The deploy fails if the value
is not a valid ISO date.

### Image provenance and SBOM

With `IMAGE_ATTESTATIONS=true`, the build uses a BuildKit container builder and attaches SLSA
provenance (`mode=max`) and an SBOM. Artifact Registry stores both. The pushed image then becomes an
OCI index (the amd64 image plus attestation manifests), and the workflow deploys that index digest.
Check on a staging project that `gcloud run deploy` accepts it before you set the variable in
production. To roll back, unset the variable.

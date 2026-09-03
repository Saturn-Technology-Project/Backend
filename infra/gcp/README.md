# Deploy Saturn to Google Cloud

Each Saturn app is a separate GitHub repo. Push `main` to deploy that service to Cloud Run.

- Backend: Cloud Run + Cloud SQL + Memorystore Redis
- Frontend: Cloud Run
- Dashboard: Cloud Run

GitHub authenticates with Workload Identity Federation. No JSON key is stored in the repo.

## One-time Google Cloud setup

Create a deploy service account and bind GitHub OIDC to it:

```bash
PROJECT_ID=your-gcp-project
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
SA=github-deployer@${PROJECT_ID}.iam.gserviceaccount.com
POOL=github
PROVIDER=saturn

gcloud iam service-accounts create github-deployer \
  --display-name "GitHub Actions deployer"

for ROLE in \
  roles/run.admin \
  roles/artifactregistry.writer \
  roles/cloudsql.admin \
  roles/redis.admin \
  roles/compute.networkAdmin \
  roles/vpcaccess.admin \
  roles/iam.serviceAccountUser
do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${SA}" \
    --role "$ROLE"
done

gcloud iam workload-identity-pools create "$POOL" \
  --location global \
  --display-name "GitHub"

gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
  --location global \
  --workload-identity-pool "$POOL" \
  --display-name "GitHub OIDC" \
  --issuer-uri "https://token.actions.githubusercontent.com" \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.owner=assertion.repository_owner" \
  --attribute-condition "assertion.repository_owner == 'YOUR_GITHUB_ORG'"

gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/YOUR_GITHUB_ORG/saturn-backend"

gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/YOUR_GITHUB_ORG/saturn-frontend"

gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/YOUR_GITHUB_ORG/saturn-dashboard"
```

Provider resource name to put in GitHub secrets:

```
projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/saturn
```

## Provision data plane

In the backend GitHub repo, set the variables below, then run **Actions → Provision Google Cloud Resources**.

Suggested values:

| Variable | Example |
|---|---|
| `GCP_PROJECT_ID` | `saturn-prod` |
| `GCP_REGION` | `asia-east1` |
| `GAR_LOCATION` | `asia-east1` |
| `GAR_REPOSITORY` | `saturn` |
| `CLOUD_SQL_INSTANCE` | `saturn-pg` |
| `CLOUD_SQL_DATABASE` | `saturn` |
| `CLOUD_SQL_USER` | `saturn` |
| `CLOUD_SQL_TIER` | `db-g1-small` |
| `REDIS_INSTANCE` | `saturn-redis` |
| `REDIS_TIER` | `BASIC` |
| `REDIS_SIZE_GB` | `1` |
| `VPC_NETWORK` | `default` |
| `SERVERLESS_VPC_CONNECTOR` | `saturn-vpc` |
| `SERVERLESS_VPC_CONNECTOR_RANGE` | `10.8.0.0/28` |

Secret: `CLOUD_SQL_PASSWORD`.

The job prints the Cloud SQL connection name and Redis host. Then set:

```
DATABASE_URL=postgresql+asyncpg://saturn:PASSWORD@/saturn?host=/cloudsql/PROJECT:REGION:INSTANCE
REDIS_URL=redis://REDIS_HOST:6379/0
CLOUD_SQL_INSTANCE_CONNECTION_NAME=PROJECT:REGION:INSTANCE
```

## GitHub variables and secrets

Shared by all three repos:

| Name | Type | Used by |
|---|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | secret | all |
| `GCP_SERVICE_ACCOUNT_EMAIL` | secret | all |
| `GCP_PROJECT_ID` | variable | all |
| `GCP_REGION` | variable | all |
| `GAR_LOCATION` | variable | all |
| `GAR_REPOSITORY` | variable | all |
| `BACKEND_PUBLIC_URL` | variable | frontend, dashboard |

Backend only:

| Name | Type |
|---|---|
| `BACKEND_CLOUD_RUN_SERVICE` | variable, e.g. `saturn-backend` |
| `CLOUD_SQL_INSTANCE_CONNECTION_NAME` | variable |
| `SERVERLESS_VPC_CONNECTOR` | variable |
| `DATABASE_URL` | secret |
| `REDIS_URL` | secret |
| `SEED_USER_EMAIL` | secret |
| `SEED_USER_PASSWORD` | secret |

Frontend / Dashboard:

| Name | Type |
|---|---|
| `FRONTEND_CLOUD_RUN_SERVICE` | variable, e.g. `saturn-frontend` |
| `DASHBOARD_CLOUD_RUN_SERVICE` | variable, e.g. `saturn-dashboard` |

`BACKEND_PUBLIC_URL` is the Cloud Run URL of the API, for example `https://saturn-backend-xxxxx.run.app`. Frontend and Dashboard bake this into the `/saturn-api` rewrite at image build time.

## Deploy

1. Run **Provision Google Cloud Resources** once from the backend repo.
2. Push `main` on backend, then copy the Cloud Run URL into `BACKEND_PUBLIC_URL`.
3. Push `main` on frontend and dashboard.

Each deploy workflow also has **Run workflow** for a manual release.

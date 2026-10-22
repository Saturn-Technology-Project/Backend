#!/usr/bin/env bash
# Steps 1-4 and 6 of "Move Redis to AUTH and TLS" in infra/gcp/README.md: a new Memorystore
# instance with AUTH and in-transit encryption, its CA and URL in Secret Manager, read access for
# the runtime account, and the GitHub variables that point the next deploy at it. The AUTH string
# is never printed. Safe to re-run. The old instance is left running: delete it and the old
# REDIS_URL secret after a deploy on the new one is healthy (step 7).
set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
REGION="${GCP_REGION:-asia-east1}"
NEW="${REDIS_INSTANCE:-saturn-redis-tls}"
NETWORK="${VPC_NETWORK:-default}"
RUNTIME_SA="${RUNTIME_SA:-saturn-runtime@${GCP_PROJECT_ID}.iam.gserviceaccount.com}"
REPO="${GITHUB_REPO:-Saturn-Technology-Project/Backend}"

if ! gcloud redis instances describe "${NEW}" --project "${GCP_PROJECT_ID}" --region "${REGION}" >/dev/null 2>&1; then
  echo "Creating ${NEW} (about 5 minutes)"
  gcloud redis instances create "${NEW}" --project "${GCP_PROJECT_ID}" --region "${REGION}" \
    --redis-version redis_7_0 --tier BASIC --size 1 \
    --network "projects/${GCP_PROJECT_ID}/global/networks/${NETWORK}" \
    --connect-mode private-service-access --enable-auth \
    --transit-encryption-mode server-authentication
fi

put_secret() {
  if gcloud secrets describe "$1" --project "${GCP_PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets versions add "$1" --project "${GCP_PROJECT_ID}" --data-file - >/dev/null
  else
    gcloud secrets create "$1" --project "${GCP_PROJECT_ID}" --replication-policy automatic --data-file - >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "$1" --project "${GCP_PROJECT_ID}" \
    --member "serviceAccount:${RUNTIME_SA}" --role roles/secretmanager.secretAccessor >/dev/null
  echo "  $1"
}

echo "Storing the server CA and the URL"
# Memorystore lists more than one certificate while it rotates its CA; keep them all.
gcloud redis instances describe "${NEW}" --project "${GCP_PROJECT_ID}" --region "${REGION}" --format=json |
  python3 -c 'import json, sys; print("\n".join(c["cert"] for c in json.load(sys.stdin)["serverCaCerts"]))' |
  put_secret REDIS_CA

HOST=$(gcloud redis instances describe "${NEW}" --project "${GCP_PROJECT_ID}" --region "${REGION}" --format='value(host)')
PORT=$(gcloud redis instances describe "${NEW}" --project "${GCP_PROJECT_ID}" --region "${REGION}" --format='value(port)')
gcloud redis instances get-auth-string "${NEW}" --project "${GCP_PROJECT_ID}" --region "${REGION}" --format='value(authString)' |
  HOST="${HOST}" PORT="${PORT}" python3 -c '
import os, sys
from urllib.parse import quote
auth = sys.stdin.read().strip()
sys.stdout.write(f"rediss://:{quote(auth, safe=\"\")}@{os.environ[\"HOST\"]}:{os.environ[\"PORT\"]}/0"
                 "?ssl_cert_reqs=required&ssl_ca_certs=/etc/secrets/redis-ca/ca.pem")' |
  put_secret REDIS_URL_TLS

echo "Pointing the next Backend deploy at ${NEW}"
gh variable set REDIS_URL_SECRET -R "${REPO}" --body REDIS_URL_TLS
gh variable set REDIS_CA_SECRET -R "${REPO}" --body REDIS_CA
echo "Done. Deploy the Backend, check sign-in and MCP sessions, then delete the old instance."

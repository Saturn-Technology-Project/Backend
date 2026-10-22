#!/usr/bin/env bash
# Rotates the Cloud SQL application password without printing it or putting it on a command line.
#   1. writes DATABASE_URL with the new password as a new Secret Manager version,
#   2. sets the password on the database user through the Cloud SQL Admin API (body on stdin),
#   3. rolls the API and the worker onto a new revision so they read the new version,
#   4. updates the CLOUD_SQL_PASSWORD GitHub secret used by provisioning.
# Between steps 2 and 3, about a minute, connections that old instances open fail; open ones
# keep working. Run as someone who can write secrets and Cloud SQL users.
set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
INSTANCE="${CLOUD_SQL_INSTANCE:-saturn-pg}"
DB_USER="${CLOUD_SQL_USER:-saturn}"
REGION="${GCP_REGION:-asia-east1}"
SECRET="${DATABASE_URL_SECRET:-DATABASE_URL}"
REPO="${GITHUB_REPO:-Saturn-Technology-Project/Backend}"
SERVICES="${SERVICES:-saturn-backend saturn-worker}"

NEW_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export NEW_PASSWORD DB_USER

echo "Writing ${SECRET} with the new password"
gcloud secrets versions access latest --secret "${SECRET}" --project "${GCP_PROJECT_ID}" |
  python3 -c '
import os, sys
from urllib.parse import quote, urlsplit, urlunsplit
url = urlsplit(sys.stdin.read().strip())
user = url.username or os.environ["DB_USER"]
host = url.hostname or ""
netloc = f"{quote(user, safe=\"\")}:{quote(os.environ[\"NEW_PASSWORD\"], safe=\"\")}@{host}"
if url.port:
    netloc += f":{url.port}"
sys.stdout.write(urlunsplit((url.scheme, netloc, url.path, url.query, url.fragment)))' |
  gcloud secrets versions add "${SECRET}" --project "${GCP_PROJECT_ID}" --data-file - >/dev/null

echo "Setting the password on ${DB_USER}"
python3 -c 'import json, os; print(json.dumps({"name": os.environ["DB_USER"], "password": os.environ["NEW_PASSWORD"]}))' |
  curl -sS --fail-with-body -o /dev/null -X PUT \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "Content-Type: application/json" \
    --data-binary @- \
    "https://sqladmin.googleapis.com/v1/projects/${GCP_PROJECT_ID}/instances/${INSTANCE}/users?name=${DB_USER}"

echo "Rolling ${SERVICES} onto the new version"
for service in ${SERVICES}; do
  gcloud run services update "${service}" --project "${GCP_PROJECT_ID}" --region "${REGION}" \
    --update-secrets "DATABASE_URL=${SECRET}:latest" --quiet >/dev/null
  echo "  ${service}"
done

if command -v gh >/dev/null 2>&1; then
  printf '%s' "${NEW_PASSWORD}" | gh secret set CLOUD_SQL_PASSWORD -R "${REPO}"
  echo "Updated the CLOUD_SQL_PASSWORD GitHub secret"
fi
unset NEW_PASSWORD
echo "Done. Older ${SECRET} versions still hold the old password; disable them once the services are healthy."

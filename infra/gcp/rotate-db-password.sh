#!/usr/bin/env bash
# Rotates the Cloud SQL application password without printing it or putting it on a command line.
#   1. sets the new password on the database user through the Cloud SQL Admin API (body on
#      stdin) and waits for that operation to finish,
#   2. writes DATABASE_URL with the new password as a new Secret Manager version,
#   3. rolls the API and the worker onto a new revision so they read the new version,
#   4. updates the CLOUD_SQL_PASSWORD GitHub secret used by provisioning.
#
# The password is changed before the secret is written. The other order left a window in which
# `DATABASE_URL:latest` named a password the database did not have yet, and left it that way
# for good if the password change then failed: every instance started after that could not
# connect. If step 2 fails here, the old password is put back, so the secret that is still
# `latest` stays correct.
#
# Between steps 1 and 3, about a minute, connections that old instances open fail; open ones
# keep working. Run as someone who can write secrets and Cloud SQL users.
set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
INSTANCE="${CLOUD_SQL_INSTANCE:-saturn-pg}"
DB_USER="${CLOUD_SQL_USER:-saturn}"
REGION="${GCP_REGION:-asia-east1}"
SECRET="${DATABASE_URL_SECRET:-DATABASE_URL}"
REPO="${GITHUB_REPO:-Saturn-Technology-Project/Saturn-MCP-Backend}"
SERVICES="${SERVICES:-saturn-backend saturn-worker}"
USERS_URL="https://sqladmin.googleapis.com/v1/projects/${GCP_PROJECT_ID}/instances/${INSTANCE}/users?name=${DB_USER}"

CURRENT_URL="$(gcloud secrets versions access latest --secret "${SECRET}" --project "${GCP_PROJECT_ID}")"
OLD_PASSWORD="$(CURRENT_URL="${CURRENT_URL}" python3 -c '
import os
from urllib.parse import unquote, urlsplit
print(unquote(urlsplit(os.environ["CURRENT_URL"].strip()).password or ""))')"
if [ -z "${OLD_PASSWORD}" ]; then
  echo "The current ${SECRET} has no password in it; nothing to rotate from." >&2
  exit 1
fi
NEW_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export DB_USER

# Sets the database user's password to the value of the named variable, and waits until Cloud
# SQL reports the change as done. The password travels in the request body on stdin and the
# access token in a header read from a file descriptor, so neither is on a command line.
set_password() {
  local variable="$1" response operation
  response="$(mktemp)"
  PASSWORD_VALUE="${!variable}" python3 -c '
import json, os
print(json.dumps({"name": os.environ["DB_USER"], "password": os.environ["PASSWORD_VALUE"]}))' |
    curl -sS --fail-with-body -o "${response}" -X PUT \
      -H @<(printf 'Authorization: Bearer %s\n' "$(gcloud auth print-access-token)") \
      -H "Content-Type: application/json" \
      --data-binary @- \
      "${USERS_URL}"
  operation="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("name", ""))' "${response}")"
  rm -f "${response}"
  if [ -z "${operation}" ]; then
    echo "Cloud SQL did not return an operation for the password change." >&2
    return 1
  fi
  gcloud sql operations wait "${operation}" --project "${GCP_PROJECT_ID}" --timeout 300 >/dev/null
}

STAGE="start"
restore() {
  local code=$?
  if [ "${STAGE}" = "password-changed" ]; then
    echo "Could not write ${SECRET}. Putting the previous password back." >&2
    if set_password OLD_PASSWORD; then
      echo "The previous password is in place again. Nothing was rotated." >&2
    else
      echo "COULD NOT RESTORE THE PASSWORD. The database has a password that no secret holds." >&2
      echo "Set one by hand (gcloud sql users set-password ${DB_USER} --instance ${INSTANCE} --prompt-for-password)" >&2
      echo "and write it to ${SECRET} before any instance restarts." >&2
    fi
  elif [ "${STAGE}" = "secret-written" ]; then
    echo "The password and ${SECRET} were both updated, but a service did not roll." >&2
    echo "Finish with: gcloud run services update <service> --update-secrets DATABASE_URL=${SECRET}:latest" >&2
  fi
  exit "${code}"
}
trap restore ERR

echo "Setting the password on ${DB_USER}"
set_password NEW_PASSWORD
STAGE="password-changed"

echo "Writing ${SECRET} with the new password"
CURRENT_URL="${CURRENT_URL}" NEW_PASSWORD="${NEW_PASSWORD}" python3 - <<'PY' | gcloud secrets versions add "${SECRET}" --project "${GCP_PROJECT_ID}" --data-file - >/dev/null
import os, sys
from urllib.parse import quote, urlsplit, urlunsplit
url = urlsplit(os.environ["CURRENT_URL"].strip())
user = url.username or os.environ["DB_USER"]
netloc = quote(user, safe="") + ":" + quote(os.environ["NEW_PASSWORD"], safe="") + "@" + (url.hostname or "")
if url.port:
    netloc += ":" + str(url.port)
sys.stdout.write(urlunsplit((url.scheme, netloc, url.path, url.query, url.fragment)))
PY
STAGE="secret-written"
unset CURRENT_URL OLD_PASSWORD

echo "Rolling ${SERVICES} onto the new version"
for service in ${SERVICES}; do
  gcloud run services update "${service}" --project "${GCP_PROJECT_ID}" --region "${REGION}" \
    --update-secrets "DATABASE_URL=${SECRET}:latest" --quiet >/dev/null
  echo "  ${service}"
done
STAGE="rolled"

if command -v gh >/dev/null 2>&1; then
  printf '%s' "${NEW_PASSWORD}" | gh secret set CLOUD_SQL_PASSWORD -R "${REPO}"
  echo "Updated the CLOUD_SQL_PASSWORD GitHub secret"
fi
unset NEW_PASSWORD
echo "Done. Older ${SECRET} versions still hold the old password; disable them once the services are healthy."

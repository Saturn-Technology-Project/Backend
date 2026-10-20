#!/usr/bin/env bash
set -euo pipefail

required_vars=(
  GCP_PROJECT_ID
  GCP_REGION
  GAR_LOCATION
  GAR_REPOSITORY
  CLOUD_SQL_INSTANCE
  CLOUD_SQL_DATABASE
  CLOUD_SQL_USER
  CLOUD_SQL_PASSWORD
  CLOUD_SQL_TIER
  CLOUD_SQL_AVAILABILITY_TYPE
  CLOUD_SQL_BACKUP_START_TIME
  REDIS_INSTANCE
  REDIS_TIER
  REDIS_SIZE_GB
  VPC_NETWORK
  VPC_CONNECTOR
  VPC_CONNECTOR_RANGE
)

for name in "${required_vars[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required env: ${name}" >&2
    exit 1
  fi
done

gcloud config set project "${GCP_PROJECT_ID}" >/dev/null

warn() {
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
    echo "::warning::$*"
  else
    echo "WARNING: $*" >&2
  fi
}

# Creates the database user. The password goes to the Cloud SQL Admin API in a
# request body read from stdin, so it never appears in a process argument list
# (ps, /proc/*/cmdline, shell history or audit logs). CLOUD_SQL_USER and
# CLOUD_SQL_PASSWORD must be exported.
create_sql_user() {
  local token response operation
  token="$(gcloud auth print-access-token)"
  if ! response="$(
    python3 -c 'import json, os; print(json.dumps({"name": os.environ["CLOUD_SQL_USER"], "password": os.environ["CLOUD_SQL_PASSWORD"]}))' |
      curl -sS --fail-with-body -X POST \
        -H @<(printf 'Authorization: Bearer %s\n' "${token}") \
        -H "Content-Type: application/json" \
        --data-binary @- \
        "https://sqladmin.googleapis.com/v1/projects/${GCP_PROJECT_ID}/instances/${CLOUD_SQL_INSTANCE}/users"
  )"; then
    echo "Creating Cloud SQL user ${CLOUD_SQL_USER} failed: ${response}" >&2
    exit 1
  fi
  operation="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])' <<<"${response}")"
  gcloud sql operations wait "${operation}" --timeout 300 >/dev/null
}

echo "Enabling Google Cloud APIs"
gcloud services enable \
  artifactregistry.googleapis.com \
  run.googleapis.com \
  sqladmin.googleapis.com \
  redis.googleapis.com \
  vpcaccess.googleapis.com \
  compute.googleapis.com \
  servicenetworking.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com

echo "Ensuring Artifact Registry repository ${GAR_REPOSITORY}"
gcloud artifacts repositories describe "${GAR_REPOSITORY}" \
  --location "${GAR_LOCATION}" >/dev/null 2>&1 || \
gcloud artifacts repositories create "${GAR_REPOSITORY}" \
  --location "${GAR_LOCATION}" \
  --repository-format docker \
  --description "Saturn container images"

echo "Ensuring private service access on ${VPC_NETWORK}"
if ! gcloud compute addresses describe google-managed-services-"${VPC_NETWORK}" \
  --global >/dev/null 2>&1; then
  gcloud compute addresses create google-managed-services-"${VPC_NETWORK}" \
    --global \
    --purpose VPC_PEERING \
    --prefix-length 16 \
    --network "${VPC_NETWORK}"
fi

if ! gcloud services vpc-peerings list \
  --network "${VPC_NETWORK}" \
  --format="value(peering)" | grep -Fx servicenetworking.googleapis.com >/dev/null 2>&1; then
  gcloud services vpc-peerings connect \
    --service servicenetworking.googleapis.com \
    --network "${VPC_NETWORK}" \
    --ranges google-managed-services-"${VPC_NETWORK}"
fi

echo "Ensuring serverless VPC connector ${VPC_CONNECTOR}"
if gcloud compute networks vpc-access connectors describe "${VPC_CONNECTOR}" \
  --region "${GCP_REGION}" >/dev/null 2>&1; then
  state="$(gcloud compute networks vpc-access connectors describe "${VPC_CONNECTOR}" \
    --region "${GCP_REGION}" --format='value(state)')"
  if [[ "${state}" != "READY" ]]; then
    echo "Connector ${VPC_CONNECTOR} is ${state}; recreating"
    gcloud compute networks vpc-access connectors delete "${VPC_CONNECTOR}" \
      --region "${GCP_REGION}" --quiet
    gcloud compute networks vpc-access connectors create "${VPC_CONNECTOR}" \
      --region "${GCP_REGION}" \
      --network "${VPC_NETWORK}" \
      --range "${VPC_CONNECTOR_RANGE}" \
      --min-instances 2 \
      --max-instances 3 \
      --machine-type e2-micro
  fi
else
  gcloud compute networks vpc-access connectors create "${VPC_CONNECTOR}" \
    --region "${GCP_REGION}" \
    --network "${VPC_NETWORK}" \
    --range "${VPC_CONNECTOR_RANGE}" \
    --min-instances 2 \
    --max-instances 3 \
    --machine-type e2-micro
fi

echo "Ensuring Cloud SQL instance ${CLOUD_SQL_INSTANCE}"
# New instances: TLS required, deletion protection, daily backups and
# point-in-time recovery. An existing instance is never changed here; see the
# "Harden an existing Cloud SQL instance" runbook in infra/gcp/README.md.
if gcloud sql instances describe "${CLOUD_SQL_INSTANCE}" >/dev/null 2>&1; then
  sql_state="$(gcloud sql instances describe "${CLOUD_SQL_INSTANCE}" \
    --format='csv[no-heading](settings.ipConfiguration.sslMode,settings.deletionProtectionEnabled,settings.backupConfiguration.enabled,settings.backupConfiguration.pointInTimeRecoveryEnabled)')"
  IFS=, read -r ssl_mode deletion_protection backups pitr <<<"${sql_state}"
  [[ "${ssl_mode}" == "ENCRYPTED_ONLY" || "${ssl_mode}" == "TRUSTED_CLIENT_CERTIFICATE_REQUIRED" ]] || \
    warn "Cloud SQL ${CLOUD_SQL_INSTANCE} accepts unencrypted connections (sslMode=${ssl_mode:-unset})"
  [[ "${deletion_protection}" == "True" ]] || warn "Cloud SQL ${CLOUD_SQL_INSTANCE} has no deletion protection"
  [[ "${backups}" == "True" ]] || warn "Cloud SQL ${CLOUD_SQL_INSTANCE} has automated backups off"
  [[ "${pitr}" == "True" ]] || warn "Cloud SQL ${CLOUD_SQL_INSTANCE} has point-in-time recovery off"
else
  gcloud sql instances create "${CLOUD_SQL_INSTANCE}" \
    --database-version POSTGRES_16 \
    --edition enterprise \
    --region "${GCP_REGION}" \
    --tier "${CLOUD_SQL_TIER}" \
    --storage-auto-increase \
    --availability-type "${CLOUD_SQL_AVAILABILITY_TYPE}" \
    --network "projects/${GCP_PROJECT_ID}/global/networks/${VPC_NETWORK}" \
    --no-assign-ip \
    --ssl-mode ENCRYPTED_ONLY \
    --deletion-protection \
    --backup-start-time "${CLOUD_SQL_BACKUP_START_TIME}" \
    --retained-backups-count 14 \
    --enable-point-in-time-recovery \
    --retained-transaction-log-days 7
fi

echo "Ensuring Cloud SQL database ${CLOUD_SQL_DATABASE}"
gcloud sql databases describe "${CLOUD_SQL_DATABASE}" \
  --instance "${CLOUD_SQL_INSTANCE}" >/dev/null 2>&1 || \
gcloud sql databases create "${CLOUD_SQL_DATABASE}" \
  --instance "${CLOUD_SQL_INSTANCE}"

echo "Ensuring Cloud SQL user ${CLOUD_SQL_USER}"
if ! gcloud sql users list \
  --instance "${CLOUD_SQL_INSTANCE}" \
  --format="value(name)" | grep -Fx "${CLOUD_SQL_USER}" >/dev/null 2>&1; then
  create_sql_user
fi

echo "Ensuring Memorystore Redis instance ${REDIS_INSTANCE}"
# New instances require AUTH and TLS (port 6378). Transit encryption cannot be
# turned on for an existing instance; see "Move Redis to AUTH and TLS" in
# infra/gcp/README.md.
if gcloud redis instances describe "${REDIS_INSTANCE}" \
  --region "${GCP_REGION}" >/dev/null 2>&1; then
  redis_state="$(gcloud redis instances describe "${REDIS_INSTANCE}" \
    --region "${GCP_REGION}" --format='csv[no-heading](authEnabled,transitEncryptionMode)')"
  IFS=, read -r redis_auth redis_tls <<<"${redis_state}"
  [[ "${redis_auth}" == "True" ]] || warn "Redis ${REDIS_INSTANCE} has AUTH off"
  [[ "${redis_tls}" == "SERVER_AUTHENTICATION" ]] || warn "Redis ${REDIS_INSTANCE} has in-transit encryption off"
else
  gcloud redis instances create "${REDIS_INSTANCE}" \
    --region "${GCP_REGION}" \
    --redis-version redis_7_0 \
    --tier "${REDIS_TIER}" \
    --size "${REDIS_SIZE_GB}" \
    --network "projects/${GCP_PROJECT_ID}/global/networks/${VPC_NETWORK}" \
    --connect-mode private-service-access \
    --enable-auth \
    --transit-encryption-mode server-authentication
fi

# The runtime identity needs IAM grants that the GitHub deployer cannot make.
# A project IAM admin runs this, or runs runtime-service-account.sh directly.
if [[ "${SETUP_RUNTIME_SERVICE_ACCOUNT:-false}" == "true" ]]; then
  bash "$(dirname "${BASH_SOURCE[0]}")/runtime-service-account.sh"
fi

echo
echo "Bootstrap complete."
echo "Cloud SQL connection name:"
gcloud sql instances describe "${CLOUD_SQL_INSTANCE}" \
  --format="value(connectionName)"
echo
echo "Redis host and port (6378 means TLS; build REDIS_URL as in infra/gcp/README.md):"
gcloud redis instances describe "${REDIS_INSTANCE}" \
  --region "${GCP_REGION}" \
  --format="value(host,port)"

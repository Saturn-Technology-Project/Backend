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
gcloud compute networks vpc-access connectors describe "${VPC_CONNECTOR}" \
  --region "${GCP_REGION}" >/dev/null 2>&1 || \
gcloud compute networks vpc-access connectors create "${VPC_CONNECTOR}" \
  --region "${GCP_REGION}" \
  --network "${VPC_NETWORK}" \
  --range "${VPC_CONNECTOR_RANGE}"

echo "Ensuring Cloud SQL instance ${CLOUD_SQL_INSTANCE}"
gcloud sql instances describe "${CLOUD_SQL_INSTANCE}" >/dev/null 2>&1 || \
gcloud sql instances create "${CLOUD_SQL_INSTANCE}" \
  --database-version POSTGRES_16 \
  --region "${GCP_REGION}" \
  --tier "${CLOUD_SQL_TIER}" \
  --storage-auto-increase \
  --availability-type zonal \
  --network "projects/${GCP_PROJECT_ID}/global/networks/${VPC_NETWORK}" \
  --no-assign-ip

echo "Ensuring Cloud SQL database ${CLOUD_SQL_DATABASE}"
gcloud sql databases describe "${CLOUD_SQL_DATABASE}" \
  --instance "${CLOUD_SQL_INSTANCE}" >/dev/null 2>&1 || \
gcloud sql databases create "${CLOUD_SQL_DATABASE}" \
  --instance "${CLOUD_SQL_INSTANCE}"

echo "Ensuring Cloud SQL user ${CLOUD_SQL_USER}"
if ! gcloud sql users list \
  --instance "${CLOUD_SQL_INSTANCE}" \
  --format="value(name)" | grep -Fx "${CLOUD_SQL_USER}" >/dev/null 2>&1; then
  gcloud sql users create "${CLOUD_SQL_USER}" \
    --instance "${CLOUD_SQL_INSTANCE}" \
    --password "${CLOUD_SQL_PASSWORD}"
fi

echo "Ensuring Memorystore Redis instance ${REDIS_INSTANCE}"
gcloud redis instances describe "${REDIS_INSTANCE}" \
  --region "${GCP_REGION}" >/dev/null 2>&1 || \
gcloud redis instances create "${REDIS_INSTANCE}" \
  --region "${GCP_REGION}" \
  --redis-version redis_7_0 \
  --tier "${REDIS_TIER}" \
  --size "${REDIS_SIZE_GB}" \
  --network "projects/${GCP_PROJECT_ID}/global/networks/${VPC_NETWORK}" \
  --connect-mode private-service-access

echo
echo "Bootstrap complete."
echo "Cloud SQL connection name:"
gcloud sql instances describe "${CLOUD_SQL_INSTANCE}" \
  --format="value(connectionName)"
echo
echo "Redis host:"
gcloud redis instances describe "${REDIS_INSTANCE}" \
  --region "${GCP_REGION}" \
  --format="value(host)"

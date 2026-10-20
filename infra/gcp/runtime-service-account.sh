#!/usr/bin/env bash
# Creates the identity the backend runs as on Cloud Run, with only what it uses:
#   - read access to each named runtime secret (not to every secret in the project)
#   - MAC signing on the single KMS key named by SATURN_KMS_KEY
#   - Cloud SQL client, and writing logs and metrics
# Without it, Cloud Run uses the default compute service account, which usually
# holds roles/editor on the whole project.
#
# Run as someone who can grant IAM (project IAM admin); the GitHub deployer
# cannot. Safe to re-run. Then set the GitHub variable RUNTIME_SERVICE_ACCOUNT
# to the email it prints. See infra/gcp/README.md, "Runtime service account".
set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
: "${SATURN_KMS_KEY:?SATURN_KMS_KEY is required: the full cryptoKeyVersion name, as in the GitHub variable}"

RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-saturn-runtime}"
RUNTIME_SA="${RUNTIME_SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
# Optional: the deployer's email. It then gets iam.serviceAccountUser on this
# account only, which is what `gcloud run deploy --service-account` needs.
DEPLOYER_SA="${DEPLOYER_SA:-}"
# Secrets the API and worker mount (deploy-cloud-run.yml). Add REDIS_URL_SECRET /
# REDIS_CA_SECRET names when those variables are used, and SEED_USER_EMAIL /
# SEED_USER_PASSWORD only while SEED_ON_DEPLOY is true.
RUNTIME_SECRETS="${RUNTIME_SECRETS:-DATABASE_URL REDIS_URL SATURN_GITHUB_OAUTH_CLIENT_ID SATURN_GITHUB_OAUTH_CLIENT_SECRET SATURN_GOOGLE_OAUTH_CLIENT_ID SATURN_GOOGLE_OAUTH_CLIENT_SECRET RESEND_API_KEY RESEND_FROM POLICY_LLM_BASE_URL POLICY_LLM_API_KEY POLICY_LLM_MODEL ORIGIN_AUTH_SECRET}"

# SATURN_KMS_KEY names a key version; IAM is granted on the key that owns it.
KMS_KEY="${SATURN_KMS_KEY%/cryptoKeyVersions/*}"
if [[ "${KMS_KEY}" != projects/*/locations/*/keyRings/*/cryptoKeys/* ]]; then
  echo "SATURN_KMS_KEY must look like projects/P/locations/L/keyRings/R/cryptoKeys/K/cryptoKeyVersions/N" >&2
  exit 1
fi

# A new service account takes a few seconds to become visible to IAM.
retry() {
  local attempt
  for attempt in 1 2 3 4 5 6; do
    if "$@" >/dev/null; then
      return 0
    fi
    echo "Retrying in $((attempt * 5))s: $*" >&2
    sleep $((attempt * 5))
  done
  "$@" >/dev/null
}

echo "Ensuring service account ${RUNTIME_SA}"
if ! gcloud iam service-accounts describe "${RUNTIME_SA}" --project "${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${RUNTIME_SA_NAME}" \
    --project "${GCP_PROJECT_ID}" \
    --display-name "Saturn backend runtime" \
    --description "Cloud Run runtime identity for the Saturn backend and its migration job"
fi

echo "Granting project roles"
for role in roles/cloudsql.client roles/logging.logWriter roles/monitoring.metricWriter; do
  retry gcloud projects add-iam-policy-binding "${GCP_PROJECT_ID}" \
    --member "serviceAccount:${RUNTIME_SA}" \
    --role "${role}" \
    --condition None
done

echo "Granting KMS MAC signing on ${KMS_KEY}"
retry gcloud kms keys add-iam-policy-binding "${KMS_KEY}" \
  --member "serviceAccount:${RUNTIME_SA}" \
  --role roles/cloudkms.signerVerifier

echo "Granting secret access"
missing=()
for secret in ${RUNTIME_SECRETS}; do
  if gcloud secrets describe "${secret}" --project "${GCP_PROJECT_ID}" >/dev/null 2>&1; then
    retry gcloud secrets add-iam-policy-binding "${secret}" \
      --project "${GCP_PROJECT_ID}" \
      --member "serviceAccount:${RUNTIME_SA}" \
      --role roles/secretmanager.secretAccessor
  else
    missing+=("${secret}")
  fi
done

if [[ -n "${DEPLOYER_SA}" ]]; then
  echo "Letting ${DEPLOYER_SA} deploy as ${RUNTIME_SA}"
  retry gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
    --project "${GCP_PROJECT_ID}" \
    --member "serviceAccount:${DEPLOYER_SA}" \
    --role roles/iam.serviceAccountUser
fi

echo
if ((${#missing[@]} > 0)); then
  echo "These secrets do not exist yet, so nothing was granted on them: ${missing[*]}"
  echo "Create them, then run this script again."
fi
echo "Runtime service account: ${RUNTIME_SA}"
echo "Set the GitHub variable RUNTIME_SERVICE_ACCOUNT to that email."

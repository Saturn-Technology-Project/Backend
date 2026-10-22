#!/usr/bin/env bash
# Second half of split-deploy-identities.sh. Run only after every repo has deployed with its
# own deployer and every service runs as its own runtime account; the checks below refuse
# otherwise. It then:
#   - removes every project role from the shared github-deployer and deletes it,
#   - lets only workflows on refs/heads/main get a Google token,
#   - takes the default compute account's access to runtime secrets, the KMS key and Cloud SQL.
# Run as a project IAM admin. See infra/gcp/README.md.
set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
: "${SATURN_KMS_KEY:?SATURN_KMS_KEY is required: the full cryptoKeyVersion name}"
GITHUB_ORG="${GITHUB_ORG:-Saturn-Technology-Project}"
REGION="${GCP_REGION:-asia-east1}"
POOL="${WIF_POOL:-github}"
PROVIDER="${WIF_PROVIDER:-saturn}"
PROJECT_NUMBER="$(gcloud projects describe "${GCP_PROJECT_ID}" --format='value(projectNumber)')"
OLD="github-deployer@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
COMPUTE="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
KMS_KEY="${SATURN_KMS_KEY%/cryptoKeyVersions/*}"

echo "Checking that nothing still runs as the default compute account"
still=$(gcloud run services list --project "${GCP_PROJECT_ID}" --region "${REGION}" \
  --format='value(metadata.name,spec.template.spec.serviceAccountName)' | awk -v c="${COMPUTE}" '$2 == c || $2 == "" {print $1}')
if [[ -n "${still}" ]]; then
  echo "These services still run as ${COMPUTE}: ${still}" >&2
  echo "Deploy them with their own runtime account first." >&2
  exit 1
fi
echo "Checking that no service was last deployed by the shared deployer"
last=$(gcloud run services list --project "${GCP_PROJECT_ID}" --region "${REGION}" --format=json |
  python3 -c 'import json,sys
for s in json.load(sys.stdin):
    if s["metadata"].get("annotations",{}).get("serving.knative.dev/lastModifier") == sys.argv[1]:
        print(s["metadata"]["name"])' "${OLD}")
if [[ -n "${last}" ]]; then
  echo "These services were last deployed by ${OLD}: ${last}" >&2
  echo "Deploy them once with their new deployer first." >&2
  exit 1
fi

echo "Removing the shared deployer"
if gcloud iam service-accounts describe "${OLD}" --project "${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  for role in $(gcloud projects get-iam-policy "${GCP_PROJECT_ID}" --flatten=bindings \
    --filter="bindings.members:serviceAccount:${OLD}" --format='value(bindings.role)'); do
    gcloud projects remove-iam-policy-binding "${GCP_PROJECT_ID}" --member "serviceAccount:${OLD}" \
      --role "${role}" --condition None >/dev/null
    echo "  removed ${role}"
  done
  gcloud iam service-accounts delete "${OLD}" --project "${GCP_PROJECT_ID}" --quiet
fi

echo "Only main may get a Google token"
gcloud iam workload-identity-pools providers update-oidc "${PROVIDER}" \
  --project "${GCP_PROJECT_ID}" --location global --workload-identity-pool "${POOL}" \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.owner=assertion.repository_owner,attribute.ref=assertion.ref" \
  --attribute-condition "assertion.repository_owner == '${GITHUB_ORG}' && assertion.ref == 'refs/heads/main'" >/dev/null

echo "Taking the default compute account's runtime access"
for secret in $(gcloud secrets list --project "${GCP_PROJECT_ID}" --format='value(name)'); do
  if gcloud secrets get-iam-policy "${secret}" --project "${GCP_PROJECT_ID}" --format=json | grep -q "serviceAccount:${COMPUTE}"; then
    gcloud secrets remove-iam-policy-binding "${secret}" --project "${GCP_PROJECT_ID}" \
      --member "serviceAccount:${COMPUTE}" --role roles/secretmanager.secretAccessor >/dev/null
    echo "  secret ${secret}"
  fi
done
if gcloud kms keys get-iam-policy "${KMS_KEY}" --format=json | grep -q "serviceAccount:${COMPUTE}"; then
  gcloud kms keys remove-iam-policy-binding "${KMS_KEY}" \
    --member "serviceAccount:${COMPUTE}" --role roles/cloudkms.signerVerifier >/dev/null
  echo "  KMS key"
fi
gcloud projects remove-iam-policy-binding "${GCP_PROJECT_ID}" --member "serviceAccount:${COMPUTE}" \
  --role roles/cloudsql.client --condition None >/dev/null 2>&1 && echo "  Cloud SQL client" || true

echo "Done."

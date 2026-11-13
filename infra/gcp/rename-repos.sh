#!/usr/bin/env bash
# Moves the Workload Identity bindings from the old repository names (Backend, Frontend) to the
# renamed ones (Saturn-MCP-Backend, Saturn-MCP-Frontend).
#
# GitHub's OIDC token carries the repository's current name in `repository`, and each deployer
# trusts `attribute.repository/ORG/NAME`. After a rename, a deploy gets no Google token until the
# new name is bound. This script:
#   1. lists each deployer's current workloadIdentityUser members,
#   2. adds a binding for the new repository name (safe to re-run),
#   3. with REMOVE_OLD=true, removes the binding for the old name.
#
# Run as a project IAM admin, or as an identity with roles/iam.serviceAccountAdmin on the three
# deployer accounts (get, getIamPolicy, setIamPolicy). A deployer cannot run it: it must not be
# able to change who may act as itself. Run it once without REMOVE_OLD, deploy both repositories from main,
# check that they serve, then run it again with REMOVE_OLD=true. A new repository created under an
# old name would otherwise inherit the old binding.
# See infra/gcp/README.md, "Renamed repositories".
set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
GITHUB_ORG="${GITHUB_ORG:-Saturn-Technology-Project}"
POOL="${WIF_POOL:-github}"
REMOVE_OLD="${REMOVE_OLD:-false}"
# Set PROJECT_NUMBER to skip the lookup, for an identity without resourcemanager.projects.get.
PROJECT_NUMBER="${PROJECT_NUMBER:-$(gcloud projects describe "${GCP_PROJECT_ID}" --format='value(projectNumber)')}"

# service account : old repository name : new repository name
MOVES=(
  "github-deployer-backend:Backend:Saturn-MCP-Backend"
  "github-provisioner:Backend:Saturn-MCP-Backend"
  "github-deployer-frontend:Frontend:Saturn-MCP-Frontend"
)

email() { echo "$1@${GCP_PROJECT_ID}.iam.gserviceaccount.com"; }
principal() {
  echo "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_ORG}/$1"
}
members() {
  gcloud iam service-accounts get-iam-policy "$(email "$1")" --project "${GCP_PROJECT_ID}" \
    --flatten=bindings --filter='bindings.role:roles/iam.workloadIdentityUser' \
    --format='value(bindings.members)' | tr ';' '\n' | sed '/^$/d'
}

for move in "${MOVES[@]}"; do
  IFS=: read -r sa old new <<<"${move}"
  echo "${sa}"
  if ! gcloud iam service-accounts describe "$(email "${sa}")" --project "${GCP_PROJECT_ID}" >/dev/null 2>&1; then
    echo "  does not exist, skipped" >&2
    continue
  fi
  current="$(members "${sa}")"
  echo "  trusted now:"
  if [[ -n "${current}" ]]; then
    printf '%s\n' "${current}" | sed 's/^/    /'
  else
    echo "    (nothing)"
  fi

  if grep -qxF "$(principal "${new}")" <<<"${current}"; then
    echo "  ${new} already bound"
  else
    gcloud iam service-accounts add-iam-policy-binding "$(email "${sa}")" --project "${GCP_PROJECT_ID}" \
      --role roles/iam.workloadIdentityUser --member "$(principal "${new}")" >/dev/null
    echo "  bound ${new}"
  fi

  if [[ "${REMOVE_OLD}" == "true" ]]; then
    if grep -qxF "$(principal "${old}")" <<<"${current}"; then
      gcloud iam service-accounts remove-iam-policy-binding "$(email "${sa}")" --project "${GCP_PROJECT_ID}" \
        --role roles/iam.workloadIdentityUser --member "$(principal "${old}")" >/dev/null
      echo "  removed ${old}"
    else
      echo "  ${old} was not bound"
    fi
  fi
done

if [[ "${REMOVE_OLD}" != "true" ]]; then
  cat <<EOF

Done. The old names are still trusted. Deploy ${GITHUB_ORG}/Saturn-MCP-Backend and
${GITHUB_ORG}/Saturn-MCP-Frontend from main (Actions, "Deploy ... to Cloud Run", Run workflow),
check that both serve, then remove the old bindings:

  REMOVE_OLD=true GCP_PROJECT_ID=${GCP_PROJECT_ID} bash infra/gcp/rename-repos.sh
EOF
fi

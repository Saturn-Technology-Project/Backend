#!/usr/bin/env bash
# Steps 1-2 of "Restrict ingress to Cloudflare" in infra/gcp/README.md: an external Application
# Load Balancer in front of the API, and a Cloud Armor policy that admits only Cloudflare's ranges.
# Needs a Cloudflare Origin CA certificate for the API hostname (SSL/TLS > Origin Server in the
# Cloudflare dashboard), saved as files. Safe to re-run; each resource is created once.
# Afterwards, by hand: point the DNS record at the printed IP (proxied, SSL mode Full (strict)),
# check https://api.saturn-core.com/health, then set CLOUD_RUN_INGRESS=internal-and-cloud-load-balancing
# on the Backend repo and deploy.
set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
: "${ORIGIN_CERT:?ORIGIN_CERT is required: path to the Cloudflare Origin CA certificate (PEM)}"
: "${ORIGIN_KEY:?ORIGIN_KEY is required: path to its private key (PEM)}"
REGION="${GCP_REGION:-asia-east1}"
SERVICE="${CLOUD_RUN_SERVICE:-saturn-backend}"
P=(--project "${GCP_PROJECT_ID}")

exists() { "$@" "${P[@]}" >/dev/null 2>&1; }

exists gcloud compute network-endpoint-groups describe saturn-backend-neg --region "${REGION}" ||
  gcloud compute network-endpoint-groups create saturn-backend-neg "${P[@]}" --region "${REGION}" \
    --network-endpoint-type serverless --cloud-run-service "${SERVICE}"
if ! exists gcloud compute backend-services describe saturn-backend-bs --global; then
  gcloud compute backend-services create saturn-backend-bs "${P[@]}" --global --load-balancing-scheme EXTERNAL_MANAGED
  gcloud compute backend-services add-backend saturn-backend-bs "${P[@]}" --global \
    --network-endpoint-group saturn-backend-neg --network-endpoint-group-region "${REGION}"
fi
exists gcloud compute url-maps describe saturn-backend-lb ||
  gcloud compute url-maps create saturn-backend-lb "${P[@]}" --default-service saturn-backend-bs
exists gcloud compute ssl-certificates describe saturn-api-origin --global ||
  gcloud compute ssl-certificates create saturn-api-origin "${P[@]}" --global \
    --certificate "${ORIGIN_CERT}" --private-key "${ORIGIN_KEY}"
exists gcloud compute target-https-proxies describe saturn-backend-https ||
  gcloud compute target-https-proxies create saturn-backend-https "${P[@]}" \
    --url-map saturn-backend-lb --ssl-certificates saturn-api-origin
exists gcloud compute addresses describe saturn-backend-ip --global ||
  gcloud compute addresses create saturn-backend-ip "${P[@]}" --global
exists gcloud compute forwarding-rules describe saturn-backend-https --global ||
  gcloud compute forwarding-rules create saturn-backend-https "${P[@]}" --global \
    --load-balancing-scheme EXTERNAL_MANAGED --address saturn-backend-ip \
    --target-https-proxy saturn-backend-https --ports 443

echo "Cloud Armor: deny by default, allow Cloudflare"
exists gcloud compute security-policies describe saturn-cloudflare-only ||
  gcloud compute security-policies create saturn-cloudflare-only "${P[@]}"
gcloud compute security-policies rules update 2147483647 "${P[@]}" --security-policy saturn-cloudflare-only --action deny-403
priority=1000
{ curl -sSf https://www.cloudflare.com/ips-v4; echo; curl -sSf https://www.cloudflare.com/ips-v6; } |
  grep . | xargs -n 10 | while read -r ranges; do
    if exists gcloud compute security-policies rules describe "${priority}" --security-policy saturn-cloudflare-only; then
      gcloud compute security-policies rules update "${priority}" "${P[@]}" --security-policy saturn-cloudflare-only \
        --src-ip-ranges "${ranges// /,}" --action allow
    else
      gcloud compute security-policies rules create "${priority}" "${P[@]}" --security-policy saturn-cloudflare-only \
        --src-ip-ranges "${ranges// /,}" --action allow
    fi
    priority=$((priority + 1))
  done
gcloud compute backend-services update saturn-backend-bs "${P[@]}" --global --security-policy saturn-cloudflare-only

echo "Load balancer IP: $(gcloud compute addresses describe saturn-backend-ip "${P[@]}" --global --format='value(address)')"

#!/usr/bin/env bash
# One-time bootstrap: create the GCS bucket that holds Terraform remote state, and write
# infra/terraform/envs/dev/backend.hcl. Run once per project, before `make tf-init`.
#
# Cost: a few KB in Standard storage (well under $0.01/month).
# This bucket is deliberately NOT managed by Terraform, so `make down` never deletes state.
#
# Usage: scripts/bootstrap_state.sh <project-id> [region]
set -euo pipefail

PROJECT_ID="${1:?usage: $0 <project-id> [region]}"
REGION="${2:-us-central1}"
BUCKET="${PROJECT_ID}-tfstate"
BACKEND_FILE="$(dirname "$0")/../infra/terraform/envs/dev/backend.hcl"

echo "Enabling Cloud Storage API in ${PROJECT_ID}..."
gcloud services enable storage.googleapis.com --project "${PROJECT_ID}"

if gcloud storage buckets describe "gs://${BUCKET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "State bucket gs://${BUCKET} already exists."
else
  echo "Creating state bucket gs://${BUCKET} in ${REGION}..."
  gcloud storage buckets create "gs://${BUCKET}" \
    --project "${PROJECT_ID}" \
    --location "${REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi

# Versioning lets you recover from a corrupted or mistakenly overwritten state file.
gcloud storage buckets update "gs://${BUCKET}" --versioning

cat > "${BACKEND_FILE}" <<EOF
bucket = "${BUCKET}"
prefix = "enterprise-rag/dev"
EOF
echo "Wrote ${BACKEND_FILE}. Next: make tf-init"

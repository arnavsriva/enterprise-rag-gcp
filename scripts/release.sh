#!/usr/bin/env bash
# Evaluation-gated release of the RAG API (BILLABLE: Cloud Build + one eval run, ~$2).
#
#   1. build the image (Cloud Build, tagged with the git commit)
#   2. deploy it as a Cloud Run revision with NO traffic, under the tag "candidate"
#   3. run the golden-set evaluation on Vertex AI Pipelines against the candidate's URL
#   4. promote the candidate to 100% of traffic only if the pipeline (and its gate) succeeds
#
# On a failed gate, live traffic stays on the current revision; the candidate stays reachable
# at its tag URL for debugging. Usage: scripts/release.sh   (or: make release)
set -euo pipefail

cd "$(dirname "$0")/.."
TF="terraform -chdir=infra/terraform/envs/dev output -raw"
PROJECT="${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
REGION="${GCP_REGION:-us-central1}"
SERVICE="$($TF api_service)"
BUCKET="$($TF bucket)"
EVAL_SA="$(terraform -chdir=infra/terraform/envs/dev output -json service_accounts | python3 -c 'import sys,json;print(json.load(sys.stdin)["eval"])')"

echo "==> 1/4 build"
make image
IMAGE="$(cat .last-image)"

echo "==> 2/4 deploy candidate (no traffic): $IMAGE"
gcloud run deploy "$SERVICE" --image "$IMAGE" --region "$REGION" --project "$PROJECT" \
  --no-traffic --tag candidate --quiet
CANDIDATE_URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" \
  --format=json | python3 -c 'import sys,json; t=[x for x in json.load(sys.stdin)["status"]["traffic"] if x.get("tag")=="candidate"]; print(t[0]["url"])')"
echo "    candidate: $CANDIDATE_URL"

echo "==> 3/4 evaluate candidate on Vertex AI Pipelines (gate enforced)"
if .venv/bin/python -m eval.pipelines.submit --image "$IMAGE" --api-url "$CANDIDATE_URL" \
    --project "$PROJECT" --region "$REGION" --bucket "$BUCKET" --service-account "$EVAL_SA" \
    --label "release-$(git log -1 --format=%h)" --wait; then
  echo "==> 4/4 gate passed: promoting candidate to 100% of traffic"
  gcloud run services update-traffic "$SERVICE" --to-tags candidate=100 \
    --region "$REGION" --project "$PROJECT" --quiet
  echo "released $IMAGE"
else
  echo "==> 4/4 gate FAILED: live traffic unchanged; candidate left at $CANDIDATE_URL" >&2
  exit 1
fi

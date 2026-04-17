#!/usr/bin/env bash
# Deploy the cloud/backend FastAPI+TiTiler service to Google Cloud Run.
#
# This script uses a **local Docker build + push to Artifact Registry**, not
# `gcloud run deploy --source`, because Cloud Build in sunstone-devel requires
# IAM grants on the default compute SA that the deploying user doesn't have.
# Pushing a pre-built image bypasses Cloud Build entirely.
#
# Co-location: both the GCS bucket (sunstone-earthengine-data) and the AR repo
# are in us-east1; keeping Cloud Run in us-east1 means same-region COG reads
# (no egress fees, lower tile latency).
set -euo pipefail

PROJECT="${GCP_PROJECT:-sunstone-devel}"
REGION="${REGION:-us-east1}"
SERVICE_NAME="${SERVICE_NAME:-kenyamap-titiler}"
BUCKET="${BUCKET:-sunstone-earthengine-data}"
COG_PREFIX="${COG_PREFIX:-results/merged_cog}"
RUNTIME_SA="${RUNTIME_SA:-alphaearth-worker@${PROJECT}.iam.gserviceaccount.com}"
AR_REPO="${AR_REPO:-cloud-run-source-deploy}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${SCRIPT_DIR}/../backend"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/${SERVICE_NAME}:latest"

echo "=== Build ==="
docker build -t "${SERVICE_NAME}:latest" "${BACKEND_DIR}"

echo "=== Tag + push to ${IMAGE} ==="
docker tag "${SERVICE_NAME}:latest" "${IMAGE}"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet --project="${PROJECT}"
docker push "${IMAGE}"

echo "=== Deploy to Cloud Run (${REGION}) ==="
gcloud run deploy "${SERVICE_NAME}" \
  --project="${PROJECT}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --port=8080 \
  --allow-unauthenticated \
  --min-instances=0 --max-instances=10 \
  --memory=2Gi --cpu=2 \
  --concurrency=80 --timeout=60s \
  --service-account="${RUNTIME_SA}" \
  --set-env-vars="BUCKET=${BUCKET},COG_PREFIX=${COG_PREFIX}"

URL=$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT}" --region="${REGION}" --format='value(status.url)')
echo ""
echo "Service URL: ${URL}"
echo ""
echo "If --allow-unauthenticated warned about IAM, a Cloud Run admin must run:"
echo "  gcloud run services add-iam-policy-binding ${SERVICE_NAME} \\"
echo "    --project=${PROJECT} --region=${REGION} \\"
echo "    --member=allUsers --role=roles/run.invoker"

#!/usr/bin/env bash
# Deploy cloud/frontend/ to Cloudflare Pages.
#
# Requires wrangler (https://developers.cloudflare.com/workers/wrangler/install-and-update/).
# Set TITILER_BASE to your Cloud Run service URL; the script rewrites config.js
# before publishing so the deployed site talks to the cloud backend, not localhost.
set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-kenyamap}"
TITILER_BASE="${TITILER_BASE:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="${SCRIPT_DIR}/../frontend"

if [[ -z "${TITILER_BASE}" ]]; then
  echo "ERROR: TITILER_BASE env var is required (Cloud Run service URL)." >&2
  echo "Example: TITILER_BASE=https://kenyamap-titiler-xxxxx.run.app bash $0" >&2
  exit 1
fi

# Sync the label mapping from the repo root in case it changed.
cp "${SCRIPT_DIR}/../../dataset_label_mapping.json" "${FRONTEND_DIR}/dataset_label_mapping.json"

# Stage a build dir so we don't mutate the checked-in config.js.
BUILD_DIR="${SCRIPT_DIR}/../.build"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"
cp -r "${FRONTEND_DIR}/." "${BUILD_DIR}/"

cat > "${BUILD_DIR}/config.js" <<EOF
window.KENYAMAP_CONFIG = {
    TITILER_BASE: '${TITILER_BASE}',
    KENYA_CENTER: [0.0236, 37.9062],
    KENYA_ZOOM: 8,
};
EOF

echo "Deploying ${BUILD_DIR} to Cloudflare Pages project '${PROJECT_NAME}'..."
wrangler pages deploy "${BUILD_DIR}" --project-name="${PROJECT_NAME}"

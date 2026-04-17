# KenyaMap Cloud Stack

Parallel deployment track using **FastAPI + TiTiler** for the backend (on Cloud Run)
and a **static frontend** (on Cloudflare Pages / GitHub Pages). COGs are read
directly from **Google Cloud Storage** over HTTP range requests — no local disk.

The original single-file `app.py` in the repo root is untouched; both stacks can
run in parallel.

## Layout

```
cloud/
├── backend/           FastAPI app (TiTiler mount + /layers resolver)
│   ├── main.py
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/          Static bundle (to be populated)
└── scripts/
    └── deploy.sh      gcloud run deploy wrapper
```

## Layer ID → GCS URL

`main.py` resolves `layer_id` (e.g. `esri_lulc_2020`, `sunstone_kenya_lulc_9C_2025`,
`similarity`) to `gs://$BUCKET/$COG_PREFIX/<filename>` using the dataset registry
mirrored from [../app.py](../app.py). Defaults:

- `BUCKET=sunstone-earthengine-data`
- `COG_PREFIX=results/merged_cog`

Frontend fetches `/layers` once at startup, caches the list, and passes the GCS
URL back to TiTiler on every `/cog/...` call.

## Local test (requires Docker + gcloud ADC)

```bash
cd cloud/backend
docker build -t kenyamap-titiler .

# Mount your gcloud credentials so GDAL can reach gs://
docker run --rm -p 8080:8080 \
  -v "$HOME/.config/gcloud:/root/.config/gcloud:ro" \
  -e GOOGLE_APPLICATION_CREDENTIALS=/root/.config/gcloud/application_default_credentials.json \
  -e BUCKET=sunstone-earthengine-data \
  -e COG_PREFIX=results/merged_cog \
  kenyamap-titiler

# In another terminal:
curl http://localhost:8080/health
curl http://localhost:8080/layers | head
curl "http://localhost:8080/cog/tilejson.json?url=gs://sunstone-earthengine-data/results/merged_cog/sunstone_kenya_lulc_2025_9Classes_assembleV1_cog.tif"
```

If the last command returns valid TileJSON with `tiles: [...]`, TiTiler can read
COGs straight from GCS — the core migration premise is proven.

## Deploy to Cloud Run

```bash
# Uses the currently active gcloud project. Override with GCP_PROJECT=... env var.
bash cloud/scripts/deploy.sh
```

After deploy, grant the Cloud Run service account **Storage Object Viewer** on
the bucket (the script prints the exact `gsutil iam ch` command).

## Frontend

Static bundle in `cloud/frontend/`:

- `index.html` — page shell (sidebar / map / toolbox); no inline CSS or JS
- `styles.css` — extracted from `generate_html()` in `../app.py`
- `app.js` — Leaflet logic, wired to `${TITILER_BASE}/cog/...` endpoints; builds
  colormaps, legends, sidebar, and class/bucket stats **client-side** from
  `dataset_label_mapping.json`
- `config.js` — runtime config, swapped at deploy time to point at the Cloud Run URL
- `dataset_label_mapping.json` — mirror of the repo-root file

### Run locally

```bash
# 1. Backend (Docker, see above) on :8088
# 2. Static frontend on :8089
cd cloud/frontend && python -m http.server 8089
# Open http://localhost:8089
```

`config.js` defaults `TITILER_BASE` to `http://localhost:8088`, so this "just works"
as long as the Docker container is up on 8088.

### Deploy to GitHub Pages (default)

A workflow at [.github/workflows/pages.yml](../.github/workflows/pages.yml)
auto-publishes `cloud/frontend/` to Pages on every push to `main` that touches
frontend files. It rewrites `config.js` with the Cloud Run `TITILER_BASE` at
build time, so the checked-in `config.js` stays at `localhost:8088` for local
dev.

**One-time setup:**

1. Push this repo to GitHub.
2. In the repo on github.com → Settings → Pages → **Source: GitHub Actions**.
3. (Optional) Settings → Secrets and variables → Actions → **Variables** tab →
   add `TITILER_BASE` with your Cloud Run URL. If unset, the workflow falls
   back to the URL baked into the YAML.
4. Push any change under `cloud/frontend/`, or click **Run workflow** on the
   Actions tab to trigger manually.

The published URL will be `https://<user>.github.io/mapViewer/` (visible in the
Actions run output and Settings → Pages).

### Deploy to Cloudflare Pages (alternative)

```bash
# First time: npm i -g wrangler && wrangler login
TITILER_BASE=https://kenyamap-titiler-720002002328.us-east1.run.app \
  bash cloud/scripts/deploy_frontend.sh
```

## Critical invariants

- `dataset_label_mapping.json` is the **only** file duplicated between local and
  cloud stacks. `deploy_frontend.sh` copies the repo-root version into the
  frontend bundle at deploy time, so keep the repo-root file authoritative.
- `cloud/backend/main.py`'s `DATASET_REGISTRY` mirrors the one in `../app.py`.
  **If you add a dataset or year to `app.py`, mirror it here.** The cloud version
  should also include any years present in GCS but absent from local `data/`
  (currently: `glad_glclu_2000`, `glc_fcs30d_2019`, `glc_fcs30d_2021`).

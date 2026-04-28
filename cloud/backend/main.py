"""FastAPI + TiTiler backend for KenyaMap cloud stack.

Serves COGs from GCS via TiTiler's built-in tile / point / statistics endpoints.
The frontend passes `url=gs://...` on every TiTiler call; `/layers` gives the
frontend the mapping from layer_id -> GCS URL at page load.

Env vars:
  BUCKET       GCS bucket name (default: sunstone-earthengine-data)
  COG_PREFIX   Object path prefix (default: results/merged_cog)
  PORT         Uvicorn port (default: 8080, Cloud Run sets this)
"""

import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from titiler.core.errors import DEFAULT_STATUS_CODES, add_exception_handlers
from titiler.core.factory import TilerFactory

BUCKET = os.environ.get("BUCKET", "sunstone-earthengine-data")
COG_PREFIX = os.environ.get("COG_PREFIX", "results/merged_cog").strip("/")

# Mirror of DATASET_REGISTRY in app.py. Keep in sync when datasets change.
# resolution_m is the native pixel size; used client-side to estimate native
# pixel count before calling /cog/statistics so the frontend can display an
# "estimated at 1/Nx resolution" note when TiTiler falls back to overviews.
DATASET_REGISTRY = {
    "sunstone_kenya_lulc_9C": {
        "display_name": "Sunstone LULC",
        "file_template": "sunstone_kenya_lulc_{year}_9Classes_assembleV1_cog.tif",
        "available_years": [2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017],
        "resolution_m": 10,
    },
    "dynamicworld": {
        "display_name": "Dynamic World",
        "file_template": "dynamicworld_{year}_cog.tif",
        "available_years": [2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017],
        "resolution_m": 10,
    },
    "esri_lulc": {
        "display_name": "ESRI LULC",
        "file_template": "esri_lulc_{year}_cog.tif",
        "available_years": [2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017],
        "resolution_m": 10,
    },
    "worldcover": {
        "display_name": "ESA WorldCover",
        "file_template": "worldcover_{year}_cog.tif",
        "available_years": [2021, 2020],
        "resolution_m": 10,
    },
    "glad_glclu": {
        "display_name": "GLAD GLCLU",
        "file_template": "glad_glclu_{year}_cog.tif",
        "available_years": [2020, 2015, 2010, 2005, 2000],
        "resolution_m": 30,
    },
    "glc_fcs30d": {
        "display_name": "GLC_FCS30D",
        "file_template": "glc_fcs30d_{year}_cog.tif",
        "available_years": [2022, 2021, 2020, 2019, 2015, 2010, 2005, 2000, 1995, 1990, 1985],
        "resolution_m": 30,
    },
    "resolve_ecoregions": {
        "display_name": "RESOLVE Ecoregions",
        "file_template": "resolve_ecoregions_{year}_cog.tif",
        "available_years": [2017],
        "resolution_m": 10,
    },
}

SIMILARITY_FILE = "alphaearth_similarity_2017_2025_30m_uint16_cog.tif"
SIMILARITY_RESOLUTION_M = 30


def _gs_url(filename: str) -> str:
    return f"gs://{BUCKET}/{COG_PREFIX}/{filename}"


def _resolve(layer_id: str) -> str:
    if layer_id == "similarity":
        return _gs_url(SIMILARITY_FILE)
    for key, cfg in DATASET_REGISTRY.items():
        prefix = key + "_"
        if not layer_id.startswith(prefix):
            continue
        try:
            year = int(layer_id[len(prefix):])
        except ValueError:
            continue
        if year in cfg["available_years"]:
            return _gs_url(cfg["file_template"].format(year=year))
    raise HTTPException(status_code=404, detail=f"Unknown layer_id: {layer_id}")


app = FastAPI(title="KenyaMap Cloud", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

cog_tiler = TilerFactory()
app.include_router(cog_tiler.router, prefix="/cog", tags=["COG"])
add_exception_handlers(app, DEFAULT_STATUS_CODES)


@app.get("/health", tags=["meta"])
def health():
    return {"ok": True, "bucket": BUCKET, "prefix": COG_PREFIX}


@app.get("/layers", tags=["meta"])
def list_layers():
    layers = []
    for key, cfg in DATASET_REGISTRY.items():
        for year in cfg["available_years"]:
            layers.append({
                "layer_id": f"{key}_{year}",
                "dataset_key": key,
                "display_name": cfg["display_name"],
                "year": year,
                "url": _gs_url(cfg["file_template"].format(year=year)),
                "resolution_m": cfg["resolution_m"],
            })
    layers.append({
        "layer_id": "similarity",
        "dataset_key": "similarity",
        "display_name": "AlphaEarth Similarity",
        "year": None,
        "url": _gs_url(SIMILARITY_FILE),
        "resolution_m": SIMILARITY_RESOLUTION_M,
    })
    return {"layers": layers}


@app.get("/layers/{layer_id}/url", tags=["meta"])
def resolve_layer(layer_id: str):
    return {"layer_id": layer_id, "url": _resolve(layer_id)}

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Summary

KenyaMap is a geospatial web application for visualizing land use/land cover (LULC) raster data over Kenya. It serves COG (Cloud Optimized GeoTIFF) files from `data/` as XYZ tiles via `localtileserver`, generates a Leaflet.js frontend with `folium`-style templating, and exposes a lightweight Python HTTP API for tile activation and pixel queries.

See `AGENTS.md` for full data specifications, dataset schemas, year availability, and implementation guide.

## Running the Application

```bash
pip install -r requirements.txt
python app.py              # starts on port 8000, opens browser
python app.py --port 9000  # custom port
python app.py --no-browser # don't auto-open browser
```

Requires GDAL system libraries on PATH. COG data files must be in `data/` (gitignored, not in repo).

## Architecture

**Single-file app** (`app.py`) that handles everything:

1. **Startup**: scans `data/` for COG files, loads `dataset_label_mapping.json`, generates `map.html` with injected sidebar/legend data, starts HTTP server
2. **HTTP server** (`MapHandler`): serves static files plus two API endpoints:
   - `GET /api/activate?layer_id=<id>` — lazily starts a `TileClient` for the requested layer, returns its tile URL
   - `GET /api/query?lat=<lat>&lng=<lng>&layer_id=<id>` — reads pixel value from raster via `rasterio`, returns class name/color
3. **Frontend** (`map.html`): generated at startup, not hand-edited. Contains inline Leaflet.js that fetches tile URLs from `/api/activate` on layer selection

Key data flow: user clicks sidebar -> JS calls `/api/activate` -> Python creates `TileClient` -> returns tile URL -> Leaflet renders tiles directly from `localtileserver` port.

## Key Conventions

- **All class definitions** (names, palettes, label mappings) live in `dataset_label_mapping.json`. Never hardcode class names or colors in code.
- **Datasets with `simplified_groups`** (`glad_glclu`, `glc_fcs30d`) must use group mappings for display, not the full 100+ class sets.
- **Categorical rasters** use nearest-neighbor resampling (MODE) and UInt8. **Continuous rasters** (similarity) use bilinear/AVERAGE and UInt16.
- **COG file naming**: `{dataset_key}_{year}_cog.tif`. Do not rename data files.
- **All rasters must be EPSG:4326.**
- `map.html` is a build artifact regenerated on each `python app.py` run. Edit the HTML template inside `generate_html()` in `app.py`, not `map.html` directly.
- Layer IDs follow the pattern `{dataset_key}_{year}` (e.g., `esri_lulc_2020`), except `similarity` which has no year suffix.

"""
KenyaMap — Kenya Land Cover & Similarity Map Viewer

Serves Cloud Optimized GeoTIFF (COG) raster data over an interactive Leaflet.js map.
Supports multiple LULC datasets (2017-2025) and a similarity change detection layer.

Usage:
    python app.py [--port PORT] [--no-browser]
    python app.py --external-ip 34.xx.xx.xx --no-browser   # GCP VM deployment
    python app.py --external-ip 34.xx.xx.xx --tile-port-start 9001 --no-browser
"""

import json
import sys
import argparse
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs
import webbrowser
import http.server
import socketserver
import threading

import math
import numpy as np
import rasterio
from rasterio.features import geometry_window, geometry_mask
from rasterio.transform import Affine
from localtileserver import TileClient

# =============================================================================
# Configuration
# =============================================================================

DATA_DIR = Path("data")
MAPPING_FILE = Path("dataset_label_mapping.json")
OUTPUT_HTML = Path("map.html")
KENYA_CENTER = [0.0236, 37.9062]
KENYA_ZOOM = 8
DEFAULT_PORT = 8000
DEFAULT_TILE_PORT_START = 8001

# Dataset registry — only years with complete .tif files (not .gstmp)
DATASET_REGISTRY = {
    "sunstone_kenya_lulc_9C": {
        "display_name": "Sunstone LULC",
        "file_template": "sunstone_kenya_lulc_{year}_9Classes_assembleV1_cog.tif",
        "available_years": [2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017],
    },
    "dynamicworld": {
        "display_name": "Dynamic World",
        "file_template": "dynamicworld_{year}_cog.tif",
        "available_years": [2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017],
    },
    "esri_lulc": {
        "display_name": "ESRI LULC",
        "file_template": "esri_lulc_{year}_cog.tif",
        "available_years": [2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017],
    },
    "worldcover": {
        "display_name": "ESA WorldCover",
        "file_template": "worldcover_{year}_cog.tif",
        "available_years": [2021, 2020],
    },
    "glad_glclu": {
        "display_name": "GLAD GLCLU",
        "file_template": "glad_glclu_{year}_cog.tif",
        "available_years": [2020, 2015, 2010, 2005],
    },
    "glc_fcs30d": {
        "display_name": "GLC_FCS30D",
        "file_template": "glc_fcs30d_{year}_cog.tif",
        "available_years": [2022, 2020, 2015, 2010, 2005, 2000, 1995, 1990, 1985],
    },
    "resolve_ecoregions": {
        "display_name": "RESOLVE Ecoregions",
        "file_template": "resolve_ecoregions_{year}_cog.tif",
        "available_years": [2017],
    },
}

SIMILARITY_FILE = "alphaearth_similarity_2017_2025_30m_uint16_cog.tif"

# =============================================================================
# Global State (populated at startup, used by HTTP handler)
# =============================================================================

_file_registry = {}      # layer_id -> file_path
_label_mappings = {}     # dataset_key -> mapping dict
_tile_clients = {}       # layer_id -> TileClient (lazily created)
_colormap_cache = {}     # dataset_key -> colormap dict
_class_lookup_cache = {} # dataset_key -> {pixel_value: (name, color)}
_external_ip = None      # set via --external-ip for GCP deployment
_next_tile_port = DEFAULT_TILE_PORT_START


# =============================================================================
# Data Discovery
# =============================================================================

def discover_files():
    """Scan data directory and return file registry + dataset list for the sidebar."""
    registry = {}
    datasets = []

    for key, config in DATASET_REGISTRY.items():
        years = []
        for year in config["available_years"]:
            filename = config["file_template"].format(year=year)
            filepath = DATA_DIR / filename
            if filepath.exists():
                layer_id = f"{key}_{year}"
                registry[layer_id] = str(filepath)
                years.append({"year": year, "layer_id": layer_id})
            else:
                print(f"  Skip: {filename} (not found)")
        if years:
            datasets.append({
                "key": key,
                "display_name": config["display_name"],
                "years": years,
            })

    sim_path = DATA_DIR / SIMILARITY_FILE
    if sim_path.exists():
        registry["similarity"] = str(sim_path)
    else:
        print(f"  Skip: {SIMILARITY_FILE} (not found)")

    return registry, datasets


# =============================================================================
# Colormap Construction
# =============================================================================

def hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)]


def get_class_lookup(dataset_key):
    """Return cached {pixel_value: (display_name, color)} for a dataset.

    For grouped datasets (glad_glclu, glc_fcs30d) each member value resolves
    to its group name/color; otherwise to the raw class name/color.
    """
    cached = _class_lookup_cache.get(dataset_key)
    if cached is not None:
        return cached

    ds = _label_mappings.get(dataset_key, {})
    lookup = {}
    if "groups" in ds:
        for g in ds["groups"]:
            for v in g["members"]:
                lookup[v] = (g["name"], g["color"])
    else:
        for cls in ds.get("classes", []):
            lookup[cls["value"]] = (cls["name"], cls["color"])

    _class_lookup_cache[dataset_key] = lookup
    return lookup


def build_colormap_dict(dataset_key):
    """Build sparse {pixel_value: [R,G,B,A]} dict for a categorical dataset."""
    if dataset_key in _colormap_cache:
        return _colormap_cache[dataset_key]

    ds = _label_mappings[dataset_key]
    cmap = {}

    if "groups" in ds:
        for g in ds["groups"]:
            rgb = hex_to_rgb(g["color"])
            for orig in g["members"]:
                if 0 <= orig <= 255:
                    cmap[orig] = rgb + [255]
    else:
        for cls in ds["classes"]:
            orig = cls["value"]
            if 0 <= orig <= 255:
                cmap[orig] = hex_to_rgb(cls["color"]) + [255]

    _colormap_cache[dataset_key] = cmap
    return cmap


def resolve_dataset_key(layer_id):
    """Extract the dataset key from a layer_id like 'esri_lulc_2020'."""
    if layer_id == "similarity":
        return "similarity"
    for key in DATASET_REGISTRY:
        if layer_id.startswith(key + "_"):
            return key
    return layer_id


# =============================================================================
# Tile URL Building
# =============================================================================

def get_tile_url(layer_id):
    """Lazily start a TileClient and return a colorized tile URL template."""
    global _next_tile_port

    if layer_id not in _file_registry:
        return None

    if layer_id not in _tile_clients:
        if _external_ip:
            port = _next_tile_port
            _next_tile_port += 1
            print(f"  Starting tile server for {layer_id} on port {port}...")
            client = TileClient(
                _file_registry[layer_id],
                port=port,
                host="0.0.0.0",
                client_host=_external_ip,
                client_port=port,
                cors_all=True,
            )
        else:
            print(f"  Starting tile server for {layer_id}...")
            client = TileClient(_file_registry[layer_id], cors_all=True)
        _tile_clients[layer_id] = client

    client = _tile_clients[layer_id]

    use_client = _external_ip is not None

    if layer_id == "similarity":
        # Continuous green → yellow → red
        return client.get_tile_url(
            indexes=[1],
            colormap='rdylgn_r',
            vmin=0,
            vmax=7200,
            client=use_client,
        )

    # Categorical LULC — build colormap dict, append to URL
    dataset_key = resolve_dataset_key(layer_id)
    cmap_dict = build_colormap_dict(dataset_key)

    # vmin=0, vmax=255 prevents auto-scaling on UInt8 data
    base_url = client.get_tile_url(indexes=[1], vmin=0, vmax=255, client=use_client)
    cmap_json = json.dumps(cmap_dict)
    return base_url + "&colormap=" + quote(cmap_json)


# =============================================================================
# Legend Data
# =============================================================================

def build_legend_data():
    """Build legend entries for each dataset (consumed by the frontend)."""
    legends = {}

    for key, ds in _label_mappings.items():
        if key.startswith("_"):
            continue

        items = ds.get("groups") or ds.get("classes", [])
        entries = [
            {"name": item["name"], "color": item["color"]}
            for item in items
            if item["name"] != "No Data"
        ]

        legends[key] = {
            "title": ds.get("name", key),
            "entries": entries,
        }

    legends["similarity"] = {
        "title": "Land Change Similarity (2017 vs 2025)",
        "type": "continuous",
        "min_label": "No Change",
        "max_label": "High Change",
        "colors": ["#00ff00", "#ffff00", "#ff0000"],
    }

    return legends


# =============================================================================
# HTML Generation
# =============================================================================

def generate_html(datasets, legend_data):
    """Generate complete Leaflet.js HTML map page."""

    # Build dataset years lookup for the time-series stepper
    dataset_years_map = {}
    for ds in datasets:
        dataset_years_map[ds["key"]] = [yi["year"] for yi in ds["years"]]

    # Build sidebar dataset HTML
    sidebar_datasets = ""
    for ds in datasets:
        sidebar_datasets += '<div class="dataset-group">\n'
        sidebar_datasets += (
            f'  <div class="dataset-header" onclick="toggleGroup(this)">'
            f'<span class="arrow">&#9656;</span> {ds["display_name"]}</div>\n'
        )
        sidebar_datasets += '  <div class="dataset-years">\n'
        for yi in ds["years"]:
            lid = yi["layer_id"]
            sidebar_datasets += (
                f'    <div class="year-item" id="yi-{lid}" '
                f"onclick=\"selectLayer('{ds['key']}', {yi['year']})\">"
                f'{yi["year"]}</div>\n'
            )
        sidebar_datasets += "  </div>\n</div>\n"

    # The main HTML template — uses __PLACEHOLDER__ markers
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KenyaMap — Kenya Land Cover Viewer</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.css" />
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.js"></script>
<script src="https://unpkg.com/shpjs@latest/dist/shp.js"></script>
<script src="https://unpkg.com/@tmcw/togeojson@4/dist/togeojson.umd.js"></script>
<script src="https://unpkg.com/jszip@3/dist/jszip.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@11/marked.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif}
.container{display:flex;height:100vh}

/* ── Sidebar ────────────────────────────────────────────────────────── */
#sidebar{
  width:10%;flex-shrink:0;background:#1a1a2e;color:#e0e0e0;
  overflow-y:auto;display:flex;flex-direction:column;z-index:1000;
}
#sidebar h1{
  font-size:1.3em;padding:16px 20px;background:#16213e;
  color:#00d2ff;letter-spacing:.5px;flex-shrink:0;
}
.sidebar-section{padding:12px 0;border-bottom:1px solid #2a2a4a}
.sidebar-section h3{
  font-size:.75em;text-transform:uppercase;letter-spacing:1px;
  color:#888;padding:0 20px 8px;
}
.dataset-header{
  padding:8px 20px;cursor:pointer;display:flex;align-items:center;
  transition:background .2s;font-size:.9em;gap:6px;
}
.dataset-header:hover{background:#2a2a4a}
.dataset-header.active{background:#0f3460}
.dataset-header .arrow{
  display:inline-block;width:14px;font-size:.7em;transition:transform .2s;
}
.dataset-header.expanded .arrow{transform:rotate(90deg)}
.dataset-years{display:none;padding:4px 0;background:#12122a}
.dataset-years.show{display:block}
.year-item{
  padding:5px 20px 5px 52px;cursor:pointer;font-size:.85em;
  color:#aaa;transition:all .15s;
}
.year-item:hover{background:#2a2a4a;color:#fff}
.year-item.active{background:#0f3460;color:#00d2ff;font-weight:600}
.sim-item{
  padding:8px 20px 8px 36px;cursor:pointer;font-size:.9em;transition:background .2s;
}
.sim-item:hover{background:#2a2a4a}
.sim-item.active{background:#0f3460;color:#00d2ff}
.basemap-option{
  padding:6px 20px 6px 36px;cursor:pointer;font-size:.85em;
  color:#aaa;transition:all .15s;
}
.basemap-option:hover{background:#2a2a4a;color:#fff}
.basemap-option.active{color:#00d2ff}
.about-item{
  padding:8px 20px;cursor:pointer;font-size:.9em;color:#aaa;
  transition:all .15s;display:flex;align-items:center;gap:8px;
}
.about-item:hover{background:#2a2a4a;color:#00d2ff}
.about-item .about-icon{font-size:1.1em;color:#00d2ff;flex-shrink:0}
.opacity-section{padding:12px 20px}
.opacity-section label{font-size:.85em;color:#aaa}
.opacity-section input[type=range]{width:100%;margin-top:6px;accent-color:#00d2ff}
.opacity-value{float:right;font-size:.85em;color:#00d2ff}
.clear-btn{
  display:none;margin:8px 20px;padding:6px 12px;background:#c0392b;
  color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:.8em;
}
.clear-btn.show{display:block}
.clear-btn:hover{background:#e74c3c}

/* ── ToolBox (right sidebar) ───────────────────────────────────────── */
#toolbox{
  width:15%;flex-shrink:0;background:#1a1a2e;color:#e0e0e0;
  overflow-y:auto;display:flex;flex-direction:column;z-index:1000;
  border-left:1px solid #2a2a4a;
}
#toolbox h2{
  font-size:1.1em;padding:14px 16px;background:#16213e;
  color:#00d2ff;letter-spacing:.5px;flex-shrink:0;
}
.tool-section{border-bottom:1px solid #2a2a4a}
.tool-section-header{
  padding:10px 16px;cursor:pointer;display:flex;
  align-items:center;gap:6px;font-size:.9em;transition:background .2s;
}
.tool-section-header:hover{background:#2a2a4a}
.tool-section-header .arrow{
  display:inline-block;width:14px;font-size:.7em;transition:transform .2s;
}
.tool-section-header.expanded .arrow{transform:rotate(90deg)}
.tool-section-body{display:none;padding:12px 16px;background:#12122a}
.tool-section-body.show{display:block}
.tool-btn{
  display:inline-block;padding:6px 14px;margin:4px 2px;
  background:#0f3460;color:#00d2ff;border:1px solid #00d2ff;
  border-radius:4px;cursor:pointer;font-size:.85em;transition:background .2s;
}
.tool-btn:hover{background:#1a5276}
.tool-btn.active{background:#00d2ff;color:#1a1a2e}
.tool-btn:disabled{opacity:.4;cursor:not-allowed}
.stats-results{margin-top:10px;font-size:.85em}
.stats-row{display:flex;align-items:center;padding:3px 0;gap:6px}
.stats-swatch{
  width:14px;height:14px;border-radius:2px;flex-shrink:0;
  border:1px solid rgba(255,255,255,.15);
}
.stats-bar{height:8px;background:#00d2ff;border-radius:2px;transition:width .3s}
.geojson-textarea{
  width:100%;height:120px;background:#0a0a1a;color:#00d2ff;
  border:1px solid #2a2a4a;border-radius:4px;font-family:monospace;
  font-size:.75em;padding:8px;resize:vertical;margin-top:8px;
}
.no-layer-msg{color:#666;font-size:.85em;font-style:italic}

/* ── Map ────────────────────────────────────────────────────────────── */
#map{flex:1;z-index:1}

/* ── Legend ──────────────────────────────────────────────────────────── */
.legend-control{
  background:rgba(255,255,255,.92);padding:10px 14px;border-radius:8px;
  box-shadow:0 2px 8px rgba(0,0,0,.3);max-height:400px;overflow-y:auto;
  min-width:160px;
}
.legend-control h4{margin:0 0 8px;font-size:.9em;color:#333}
.legend-entry{display:flex;align-items:center;margin:3px 0;font-size:.85em;color:#444}
.legend-swatch{
  width:16px;height:16px;border-radius:2px;margin-right:8px;
  flex-shrink:0;border:1px solid rgba(0,0,0,.15);
}
.legend-gradient{
  width:100%;height:16px;border-radius:2px;margin:6px 0 4px;
  border:1px solid rgba(0,0,0,.15);
}
.legend-gradient-labels{
  display:flex;justify-content:space-between;font-size:.75em;color:#666;
}

/* ── Loading overlay ────────────────────────────────────────────────── */
.loading-overlay{
  display:none;position:absolute;top:50%;left:50%;
  transform:translate(-50%,-50%);z-index:2000;
  background:rgba(0,0,0,.7);color:#fff;padding:16px 24px;
  border-radius:8px;font-size:.9em;pointer-events:none;
}
.loading-overlay.show{display:block}

/* ── Datasets info modal ────────────────────────────────────────────── */
.info-modal{
  display:none;position:fixed;inset:0;z-index:3000;
  background:rgba(0,0,0,.6);align-items:center;justify-content:center;
  padding:24px;
}
.info-modal.show{display:flex}
.info-modal-card{
  position:relative;background:#1a1a2e;color:#e0e0e0;
  border:1px solid #2a2a4a;border-radius:8px;
  max-width:820px;width:100%;max-height:85vh;overflow:hidden;
  box-shadow:0 8px 32px rgba(0,0,0,.5);display:flex;flex-direction:column;
}
.info-modal-close{
  position:absolute;top:10px;right:14px;background:transparent;
  border:none;color:#888;font-size:1.8em;line-height:1;cursor:pointer;
  padding:4px 10px;border-radius:4px;transition:all .15s;z-index:1;
}
.info-modal-close:hover{color:#00d2ff;background:#2a2a4a}
.info-modal-body{
  padding:28px 36px;overflow-y:auto;font-size:.95em;line-height:1.6;
}
.info-modal-body h1{
  font-size:1.5em;color:#00d2ff;margin:0 0 16px;
  padding-bottom:8px;border-bottom:1px solid #2a2a4a;
}
.info-modal-body h2{font-size:1.15em;color:#00d2ff;margin:20px 0 10px}
.info-modal-body h3{
  font-size:1em;color:#e0e0e0;margin:16px 0 8px;
  text-transform:none;letter-spacing:0;padding:0;
}
.info-modal-body p{margin:0 0 12px}
.info-modal-body strong{color:#fff}
.info-modal-body a{color:#00d2ff;text-decoration:underline}
.info-modal-body table{
  width:100%;border-collapse:collapse;margin:12px 0;font-size:.88em;
}
.info-modal-body th,.info-modal-body td{
  padding:6px 10px;border:1px solid #2a2a4a;text-align:left;
}
.info-modal-body th{background:#12122a;color:#00d2ff;font-weight:600}
.info-modal-body tr:nth-child(even) td{background:#12122a}
.info-modal-body code{
  background:#12122a;color:#00d2ff;padding:2px 5px;
  border-radius:3px;font-size:.9em;
}
.info-modal-body ul,.info-modal-body ol{margin:0 0 12px 24px}
.info-modal-body .dataset-details-link{
  display:inline-block;margin:4px 0 0;padding:3px 10px;
  background:#12122a;color:#00d2ff;border:1px solid #2a2a4a;
  border-radius:12px;font-size:.85em;text-decoration:none;cursor:pointer;
}
.info-modal-body .dataset-details-link:hover{background:#2a2a4a}
#details-modal{z-index:3100}

/* ── Pixel popup ────────────────────────────────────────────────────── */
.pixel-info{font-size:13px;line-height:1.6}
.pixel-info .pi-class{font-weight:600}
.pixel-info .pi-swatch{
  display:inline-block;width:12px;height:12px;border-radius:2px;
  margin-right:4px;vertical-align:middle;border:1px solid rgba(0,0,0,.2);
}
</style>
</head>
<body>
<div class="container">

<!-- ── Sidebar ──────────────────────────────────────────────────────── -->
<div id="sidebar">
  <h1>KenyaMap</h1>

  <div class="sidebar-section">
    <div class="about-item" onclick="showDatasetsInfo()">
      <span class="about-icon">&#9432;</span> About the Datasets
    </div>
  </div>

  <div class="sidebar-section">
    <h3>Basemap</h3>
    <div class="basemap-option" id="bm-google" onclick="setBasemap('google')">
      Google Satellite
    </div>
    <div class="basemap-option active" id="bm-esri" onclick="setBasemap('esri')">
      ESRI World Imagery
    </div>
    <div class="basemap-option" id="bm-osm" onclick="setBasemap('osm')">
      OpenStreetMap
    </div>
  </div>

  <div class="sidebar-section">
    <h3>Land Use / Land Cover Layers</h3>
    __SIDEBAR_DATASETS__
  </div>

  <div class="sidebar-section">
    <h3>Change Detection</h3>
    <div class="sim-item" id="sim-item" onclick="selectLayer('similarity',null)">
      Similarity (2017 vs 2025)
    </div>
  </div>

  <div class="sidebar-section">
    <button class="clear-btn" id="clear-btn" onclick="clearActiveLayer()">
      Clear Active Layer
    </button>
  </div>

  <div class="sidebar-section opacity-section">
    <label>Opacity <span class="opacity-value" id="opacity-val">80%</span></label>
    <input type="range" id="opacity-slider" min="0" max="100" value="80"
           oninput="setOpacity(this.value)">
  </div>
</div>

<!-- ── Map ──────────────────────────────────────────────────────────── -->
<div id="map">
  <div class="loading-overlay" id="loading">Loading tiles&hellip;</div>
</div>

<!-- ── ToolBox (right sidebar) ─────────────────────────────────────── -->
<div id="toolbox">
  <h2>ToolBox</h2>

  <!-- Tool 1: GeoJSON Export -->
  <div class="tool-section">
    <div class="tool-section-header" onclick="toggleToolSection(this)">
      <span class="arrow">&#9656;</span> GeoJSON Import/Export
    </div>
    <div class="tool-section-body">
      <button class="tool-btn" id="geojson-point-btn" onclick="startGeoJSONDraw('point')">Draw Point</button>
      <button class="tool-btn" id="geojson-rect-btn" onclick="startGeoJSONDraw('rectangle')">Draw Rectangle</button>
      <button class="tool-btn" id="geojson-poly-btn" onclick="startGeoJSONDraw('polygon')">Draw Polygon</button>
      <button class="tool-btn" id="geojson-upload-btn" onclick="document.getElementById('geojson-file-input').click()">Upload File</button>
      <input type="file" id="geojson-file-input" accept=".json,.geojson,.zip,.shp,.kml,.kmz" style="display:none" onchange="handleFileUpload(this)">
      <button class="tool-btn" id="geojson-kenya-btn" onclick="loadKenya('geojson')">Kenya</button>
      <button class="tool-btn" id="geojson-clear-btn" onclick="clearGeoJSON()" style="display:none">Clear</button>
      <textarea class="geojson-textarea" id="geojson-output" readonly
                placeholder="Draw a shape to see GeoJSON here..."></textarea>
      <button class="tool-btn" id="geojson-copy-btn" onclick="copyGeoJSON()" style="display:none">Copy to Clipboard</button>
      <button class="tool-btn" id="geojson-save-btn" onclick="saveGeoJSON()" style="display:none">Save as JSON</button>
    </div>
  </div>

  <!-- Tool 2: Area Statistics -->
  <div class="tool-section">
    <div class="tool-section-header" onclick="toggleToolSection(this)">
      <span class="arrow">&#9656;</span> Area Statistics
    </div>
    <div class="tool-section-body">
      <div id="stats-no-layer" class="no-layer-msg">Select a layer first</div>
      <div id="stats-controls" style="display:none">
        <button class="tool-btn" id="stats-draw-btn" onclick="startStatsDraw('rect')">Draw Rectangle</button>
        <button class="tool-btn" id="stats-poly-btn" onclick="startStatsDraw('poly')">Draw Polygon</button>
        <button class="tool-btn" id="stats-upload-btn" onclick="document.getElementById('stats-file-input').click()">Upload File</button>
        <input type="file" id="stats-file-input" accept=".json,.geojson,.zip,.shp,.kml,.kmz" style="display:none" onchange="handleStatsFileUpload(this)">
        <button class="tool-btn" id="stats-kenya-btn" onclick="loadKenya('stats')">Kenya</button>
        <button class="tool-btn" id="stats-clear-btn" onclick="clearStatsRect()" style="display:none">Clear</button>
        <div id="stats-loading" style="display:none;color:#888;font-size:.8em;margin-top:8px">
          Computing statistics...
        </div>
        <div id="stats-results" class="stats-results"></div>
        <div id="stats-stepper" style="display:none;margin-top:10px;padding-top:10px;border-top:1px solid #2a2a4a">
          <div style="font-size:.8em;color:#888;text-align:center;margin-bottom:6px">Time-Series Step</div>
          <div style="display:flex;align-items:center;gap:10px">
            <button class="tool-btn" id="stats-step-back" onclick="statsStepYear(-1)">&#9664;</button>
            <div id="stats-step-year" style="flex:1;text-align:center;font-size:1.1em;color:#00d2ff;font-weight:600">----</div>
            <button class="tool-btn" id="stats-step-fwd" onclick="statsStepYear(1)">&#9654;</button>
          </div>
          <div id="stats-step-range" style="font-size:.75em;color:#666;text-align:center;margin-top:4px"></div>
        </div>
      </div>
    </div>
  </div>
</div>

</div><!-- /container -->

<!-- ── Datasets info modal ──────────────────────────────────────────── -->
<div id="info-modal" class="info-modal" onclick="hideDatasetsInfo(event)">
  <div class="info-modal-card" onclick="event.stopPropagation()">
    <button class="info-modal-close" onclick="hideDatasetsInfo()" aria-label="Close">&times;</button>
    <div class="info-modal-body" id="info-modal-body">Loading&hellip;</div>
  </div>
</div>

<!-- ── Per-dataset details modal (e.g. GLAD GLCLU classes) ─────────── -->
<div id="details-modal" class="info-modal" onclick="hideDetailsModal(event)">
  <div class="info-modal-card" onclick="event.stopPropagation()">
    <button class="info-modal-close" onclick="hideDetailsModal()" aria-label="Close">&times;</button>
    <div class="info-modal-body" id="details-modal-body">Loading&hellip;</div>
  </div>
</div>

<script>
// =====================================================================
// Configuration (injected by Python)
// =====================================================================
var LEGEND_DATA = __LEGEND_DATA__;
var DATASET_YEARS = __DATASET_YEARS__;

// =====================================================================
// Map Initialization
// =====================================================================
var map = L.map('map', {
    center: __KENYA_CENTER__,
    zoom: __KENYA_ZOOM__,
    zoomControl: true
});

var basemaps = {
    esri: L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        {attribution:'Tiles &copy; Esri', maxZoom:18}
    ),
    osm: L.tileLayer(
        'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
        {attribution:'&copy; OpenStreetMap contributors', maxZoom:19}
    ),
    google: L.tileLayer(
        'https://mt{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',
        {
            attribution:'Imagery &copy; Google, Airbus, Maxar Technologies',
            subdomains:['0','1','2','3'],
            maxZoom:20
        }
    )
};
basemaps.esri.addTo(map);

// =====================================================================
// State
// =====================================================================
var activeOverlay  = null;   // {datasetKey, year, layerId, tileLayer}
var currentOpacity = 0.8;
var currentBasemap = 'esri';
var tileUrlCache   = {};     // layerId -> tileUrl

// =====================================================================
// Legend Control
// =====================================================================
var legendControl = L.control({position:'topright'});
legendControl.onAdd = function(){
    this._div = L.DomUtil.create('div','legend-control');
    this._div.style.display = 'none';
    L.DomEvent.disableClickPropagation(this._div);
    L.DomEvent.disableScrollPropagation(this._div);
    return this._div;
};
legendControl.addTo(map);

function updateLegend(datasetKey){
    var div = legendControl._div;
    if(!datasetKey || !LEGEND_DATA[datasetKey]){
        div.style.display='none'; return;
    }
    var d = LEGEND_DATA[datasetKey], html = '<h4>'+d.title+'</h4>';
    if(d.type==='continuous'){
        html += '<div class="legend-gradient" style="background:linear-gradient(to right,'+d.colors.join(',')+')"></div>';
        html += '<div class="legend-gradient-labels"><span>'+d.min_label+'</span><span>'+d.max_label+'</span></div>';
    } else {
        for(var i=0;i<d.entries.length;i++){
            var e=d.entries[i];
            html += '<div class="legend-entry"><div class="legend-swatch" style="background:'+e.color+'"></div><span>'+e.name+'</span></div>';
        }
    }
    div.innerHTML = html;
    div.style.display = 'block';
}

// =====================================================================
// Layer Management
// =====================================================================
function showLoading(){document.getElementById('loading').classList.add('show')}
function hideLoading(){document.getElementById('loading').classList.remove('show')}

function selectLayer(datasetKey, year){
    var layerId = year !== null ? datasetKey+'_'+year : datasetKey;
    if(activeOverlay && activeOverlay.layerId === layerId){ clearActiveLayer(); return; }

    if(tileUrlCache[layerId]){
        applyLayer(datasetKey, year, layerId, tileUrlCache[layerId]);
        return;
    }

    showLoading();
    fetch('/api/activate?layer_id='+encodeURIComponent(layerId))
        .then(function(r){return r.json()})
        .then(function(data){
            hideLoading();
            if(data.error){alert('Error: '+data.error);return}
            tileUrlCache[layerId] = data.tile_url;
            applyLayer(datasetKey, year, layerId, data.tile_url);
        })
        .catch(function(err){hideLoading();alert('Failed: '+err)});
}

function applyLayer(datasetKey, year, layerId, tileUrl){
    if(activeOverlay && activeOverlay.tileLayer) map.removeLayer(activeOverlay.tileLayer);

    var layer = L.tileLayer(tileUrl, {
        opacity: currentOpacity,
        maxZoom: 18,
        crossOrigin: true
    });
    layer.addTo(map);
    activeOverlay = {datasetKey:datasetKey, year:year, layerId:layerId, tileLayer:layer};

    updateLegend(datasetKey);
    updateSidebarHighlight(layerId);
    document.getElementById('clear-btn').classList.add('show');
    updateStatsVisibility();

    // If a stats query is pending, recompute for the new layer (keeps drawn geometry)
    if(lastStatsQuery){
        if(lastStatsQuery.type==='bbox') fetchAreaStats(lastStatsQuery.bounds, layerId);
        else fetchAreaStatsGeometry(lastStatsQuery.geojson, layerId);
    }
}

function clearActiveLayer(){
    if(activeOverlay && activeOverlay.tileLayer) map.removeLayer(activeOverlay.tileLayer);
    activeOverlay = null;
    updateLegend(null);
    updateSidebarHighlight(null);
    document.getElementById('clear-btn').classList.remove('show');
    updateStatsVisibility();
}

// =====================================================================
// Sidebar Interaction
// =====================================================================
function toggleGroup(el){
    el.classList.toggle('expanded');
    el.nextElementSibling.classList.toggle('show');
}

function updateSidebarHighlight(activeLayerId){
    document.querySelectorAll('.dataset-header').forEach(function(h){h.classList.remove('active')});
    document.querySelectorAll('.year-item').forEach(function(y){y.classList.remove('active')});
    document.getElementById('sim-item').classList.remove('active');

    if(!activeLayerId) return;

    if(activeLayerId==='similarity'){
        document.getElementById('sim-item').classList.add('active');
        return;
    }

    var el = document.getElementById('yi-'+activeLayerId);
    if(el){
        el.classList.add('active');
        var group = el.closest('.dataset-group');
        if(group){
            var hdr = group.querySelector('.dataset-header');
            hdr.classList.add('active','expanded');
            group.querySelector('.dataset-years').classList.add('show');
        }
    }
}

// =====================================================================
// Basemap & Opacity
// =====================================================================
function setBasemap(name){
    if(name===currentBasemap) return;
    map.removeLayer(basemaps[currentBasemap]);
    basemaps[name].addTo(map);
    // Ensure overlay stays on top
    if(activeOverlay && activeOverlay.tileLayer) activeOverlay.tileLayer.bringToFront();
    currentBasemap = name;
    document.querySelectorAll('.basemap-option').forEach(function(o){o.classList.remove('active')});
    document.getElementById('bm-'+name).classList.add('active');
}

function setOpacity(val){
    currentOpacity = val/100;
    document.getElementById('opacity-val').textContent = val+'%';
    if(activeOverlay && activeOverlay.tileLayer) activeOverlay.tileLayer.setOpacity(currentOpacity);
}

// =====================================================================
// Click / Pixel Inspector
// =====================================================================
function queryEsriImageryMeta(lat, lng){
    var bounds = map.getBounds(), size = map.getSize();
    var geom = JSON.stringify({x: lng, y: lat, spatialReference: {wkid: 4326}});
    var url = 'https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/identify'
        + '?f=json&geometryType=esriGeometryPoint&sr=4326&layers=all:0&tolerance=0&returnGeometry=false'
        + '&geometry=' + encodeURIComponent(geom)
        + '&mapExtent=' + bounds.getWest() + ',' + bounds.getSouth() + ',' + bounds.getEast() + ',' + bounds.getNorth()
        + '&imageDisplay=' + size.x + ',' + size.y + ',96';
    return fetch(url).then(function(r){return r.json()}).then(function(d){
        if(!d.results || !d.results.length) return null;
        var r0 = d.results[0], a = r0.attributes || {};
        var date = a.SRC_DATE2 || a.SRC_DATE || a['DATE (YYYYMMDD)'] || null;
        if(!date) return null;
        return {date: String(date), source: r0.layerName || a.DESCRIPTION || 'ESRI World Imagery'};
    }).catch(function(){return null});
}

map.on('click', function(e){
    if(activeDrawMode) return;
    var lat = e.latlng.lat, lng = e.latlng.lng;
    var pixelP = activeOverlay
        ? fetch('/api/query?lat='+lat.toFixed(6)+'&lng='+lng.toFixed(6)+'&layer_id='+encodeURIComponent(activeOverlay.layerId))
            .then(function(r){return r.json()}).catch(function(){return null})
        : Promise.resolve(null);
    var esriP = currentBasemap === 'esri' ? queryEsriImageryMeta(lat, lng) : Promise.resolve(null);

    Promise.all([pixelP, esriP]).then(function(parts){
        var d = parts[0], esri = parts[1];
        var hasPixel = d && !d.error;
        var isGoogle = currentBasemap === 'google';
        if(!hasPixel && !esri && !isGoogle) return;
        var c = '<div class="pixel-info">';
        if(d && !d.error){
            if(d.class_name){
                c += '<div><span class="pi-swatch" style="background:'+d.color+'"></span>';
                c += '<span class="pi-class">'+d.class_name+'</span></div>';
            } else if(d.description){
                c += '<div><span class="pi-swatch" style="background:'+d.color+'"></span>';
                c += '<span class="pi-class">'+d.description+'</span></div>';
            }
            c += '<div>Pixel value: '+d.value+'</div>';
        }
        if(esri){
            c += '<div style="margin-top:4px;padding-top:4px;border-top:1px solid #eee;font-size:12px;color:#555">';
            c += 'World Imagery Date: '+esri.date+'</div>';
        }
        c += '<div style="color:#888;font-size:11px">'+lat.toFixed(6)+', '+lng.toFixed(6)+'</div></div>';
        L.popup().setLatLng(e.latlng).setContent(c).openOn(map);
    });
});

// =====================================================================
// ToolBox — Base Setup
// =====================================================================
var drawnItems = new L.FeatureGroup();
map.addLayer(drawnItems);
var activeDrawMode = null;

function toggleToolSection(el){
    el.classList.toggle('expanded');
    el.nextElementSibling.classList.toggle('show');
}

// =====================================================================
// ToolBox — GeoJSON Export
// =====================================================================
var geojsonLayer = null;

function startGeoJSONDraw(type){
    clearGeoJSON();
    if(type==='point'){
        activeDrawMode='geojson-point';
        new L.Draw.Marker(map,{}).enable();
        document.getElementById('geojson-point-btn').classList.add('active');
    } else if(type==='polygon'){
        activeDrawMode='geojson-poly';
        new L.Draw.Polygon(map,{
            shapeOptions:{color:'#ff6b6b',weight:2,fillOpacity:.1}
        }).enable();
        document.getElementById('geojson-poly-btn').classList.add('active');
    } else {
        activeDrawMode='geojson-rect';
        new L.Draw.Rectangle(map,{
            shapeOptions:{color:'#ff6b6b',weight:2,fillOpacity:.1}
        }).enable();
        document.getElementById('geojson-rect-btn').classList.add('active');
    }
}
function handleGeoJSONDrawn(e){
    geojsonLayer = e.layer;
    drawnItems.addLayer(geojsonLayer);
    document.getElementById('geojson-point-btn').classList.remove('active');
    document.getElementById('geojson-rect-btn').classList.remove('active');
    document.getElementById('geojson-poly-btn').classList.remove('active');
    document.getElementById('geojson-clear-btn').style.display='inline-block';
    document.getElementById('geojson-copy-btn').style.display='inline-block';
    document.getElementById('geojson-save-btn').style.display='inline-block';

    var geojson;
    if(e.layerType==='marker'){
        var ll=e.layer.getLatLng();
        geojson={type:'Point',coordinates:[parseFloat(ll.lng.toFixed(6)),parseFloat(ll.lat.toFixed(6))]};
    } else if(e.layerType==='polygon'){
        var latlngs=e.layer.getLatLngs()[0];
        var coords=latlngs.map(function(ll){return[parseFloat(ll.lng.toFixed(6)),parseFloat(ll.lat.toFixed(6))]});
        coords.push(coords[0]);
        geojson={type:'Polygon',coordinates:[coords]};
    } else {
        var b=e.layer.getBounds();
        geojson={type:'Polygon',coordinates:[[
            [parseFloat(b.getWest().toFixed(6)),parseFloat(b.getSouth().toFixed(6))],
            [parseFloat(b.getEast().toFixed(6)),parseFloat(b.getSouth().toFixed(6))],
            [parseFloat(b.getEast().toFixed(6)),parseFloat(b.getNorth().toFixed(6))],
            [parseFloat(b.getWest().toFixed(6)),parseFloat(b.getNorth().toFixed(6))],
            [parseFloat(b.getWest().toFixed(6)),parseFloat(b.getSouth().toFixed(6))]
        ]]};
    }
    document.getElementById('geojson-output').value=JSON.stringify(geojson,null,2);
}
function clearGeoJSON(){
    if(geojsonLayer){drawnItems.removeLayer(geojsonLayer);geojsonLayer=null}
    document.getElementById('geojson-output').value='';
    document.getElementById('geojson-clear-btn').style.display='none';
    document.getElementById('geojson-copy-btn').style.display='none';
    document.getElementById('geojson-save-btn').style.display='none';
    document.getElementById('geojson-point-btn').classList.remove('active');
    document.getElementById('geojson-rect-btn').classList.remove('active');
    document.getElementById('geojson-poly-btn').classList.remove('active');
}
function copyGeoJSON(){
    var ta=document.getElementById('geojson-output');
    navigator.clipboard.writeText(ta.value).then(function(){
        var btn=document.getElementById('geojson-copy-btn');
        btn.textContent='Copied!';
        setTimeout(function(){btn.textContent='Copy to Clipboard'},1500);
    });
}
function saveGeoJSON(){
    var text=document.getElementById('geojson-output').value;
    if(!text) return;
    var blob=new Blob([text],{type:'application/json'});
    var a=document.createElement('a');
    a.href=URL.createObjectURL(blob);
    a.download='geometry.json';
    a.click();
    URL.revokeObjectURL(a.href);
}
// Parse an uploaded geometry file into a GeoJSON object.
// Supports: .json/.geojson, .zip/.shp (shapefile), .kml, .kmz.
function parseGeoFile(file){
    return new Promise(function(resolve,reject){
        var name=file.name.toLowerCase();
        var reader=new FileReader();
        reader.onerror=function(){reject(new Error('Read failed'))};

        if(name.endsWith('.zip')||name.endsWith('.shp')){
            reader.onload=function(e){shp(e.target.result).then(resolve,reject)};
            reader.readAsArrayBuffer(file);
        } else if(name.endsWith('.kmz')){
            reader.onload=function(e){
                JSZip.loadAsync(e.target.result).then(function(zip){
                    var kmlName=Object.keys(zip.files).find(function(n){
                        return !zip.files[n].dir && n.toLowerCase().endsWith('.kml');
                    });
                    if(!kmlName) return reject(new Error('No .kml inside .kmz'));
                    return zip.files[kmlName].async('text').then(function(kmlText){
                        var dom=new DOMParser().parseFromString(kmlText,'text/xml');
                        resolve(toGeoJSON.kml(dom));
                    });
                }).catch(reject);
            };
            reader.readAsArrayBuffer(file);
        } else if(name.endsWith('.kml')){
            reader.onload=function(e){
                try{
                    var dom=new DOMParser().parseFromString(e.target.result,'text/xml');
                    resolve(toGeoJSON.kml(dom));
                }catch(err){reject(err)}
            };
            reader.readAsText(file);
        } else {
            reader.onload=function(e){
                try{resolve(JSON.parse(e.target.result))}
                catch(err){reject(err)}
            };
            reader.readAsText(file);
        }
    });
}

function handleFileUpload(input){
    if(!input.files||!input.files[0]) return;
    parseGeoFile(input.files[0])
        .then(displayGeoJSON)
        .catch(function(err){alert('Failed to parse file: '+err)});
    input.value='';
}

// Fetch the bundled Kenya country outline and route it to either the
// GeoJSON Import/Export or Area Statistics pipeline.
function loadKenya(target){
    if(target==='stats' && !activeOverlay) return;
    fetch('gadm41_KEN_0.json')
        .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
        .then(function(gj){
            if(target==='stats') applyStatsGeometry(gj);
            else displayGeoJSON(gj);
        })
        .catch(function(err){alert('Failed to load Kenya outline: '+err)});
}
function displayGeoJSON(geojson){
    clearGeoJSON();
    geojsonLayer=L.geoJSON(geojson,{
        style:function(){return{color:'#ff6b6b',weight:2,fillOpacity:.1}},
        pointToLayer:function(f,ll){return L.marker(ll)}
    }).addTo(map);
    drawnItems.addLayer(geojsonLayer);
    map.fitBounds(geojsonLayer.getBounds());
    document.getElementById('geojson-output').value=JSON.stringify(geojson,null,2);
    document.getElementById('geojson-clear-btn').style.display='inline-block';
    document.getElementById('geojson-copy-btn').style.display='inline-block';
    document.getElementById('geojson-save-btn').style.display='inline-block';
}

// =====================================================================
// ToolBox — Area Statistics
// =====================================================================
var statsRect = null;
var lastStatsQuery = null; // {type:'bbox',bounds:L.LatLngBounds} or {type:'geometry',geojson:obj}

function updateStatsVisibility(){
    var noLayer=document.getElementById('stats-no-layer');
    var controls=document.getElementById('stats-controls');
    if(activeOverlay){
        noLayer.style.display='none';
        controls.style.display='block';
    } else {
        noLayer.style.display='block';
        controls.style.display='none';
        clearStatsRect();
    }
    document.getElementById('stats-results').innerHTML='';
}
function startStatsDraw(type){
    if(!activeOverlay) return;
    clearStatsRect();
    if(type==='poly'){
        activeDrawMode='stats-poly';
        new L.Draw.Polygon(map,{
            shapeOptions:{color:'#00d2ff',weight:2,fillOpacity:.15}
        }).enable();
        document.getElementById('stats-poly-btn').classList.add('active');
    } else {
        activeDrawMode='stats';
        new L.Draw.Rectangle(map,{
            shapeOptions:{color:'#00d2ff',weight:2,fillOpacity:.15}
        }).enable();
        document.getElementById('stats-draw-btn').classList.add('active');
    }
}
function clearStatsRect(){
    if(statsRect){drawnItems.removeLayer(statsRect);statsRect=null}
    document.getElementById('stats-results').innerHTML='';
    document.getElementById('stats-clear-btn').style.display='none';
    document.getElementById('stats-draw-btn').classList.remove('active');
    document.getElementById('stats-poly-btn').classList.remove('active');
    document.getElementById('stats-stepper').style.display='none';
    lastStatsQuery=null;
}
function handleStatsFileUpload(input){
    if(!input.files||!input.files[0]||!activeOverlay) return;
    parseGeoFile(input.files[0])
        .then(applyStatsGeometry)
        .catch(function(err){alert('Failed to parse file: '+err)});
    input.value='';
}
function applyStatsGeometry(geojson){
    clearStatsRect();
    statsRect=L.geoJSON(geojson,{
        style:function(){return{color:'#00d2ff',weight:2,fillOpacity:.15}}
    }).addTo(map);
    drawnItems.addLayer(statsRect);
    map.fitBounds(statsRect.getBounds());
    document.getElementById('stats-clear-btn').style.display='inline-block';
    fetchAreaStatsGeometry(geojson);
}
function fetchAreaStats(bounds,layerIdOverride){
    var lid=layerIdOverride||activeOverlay.layerId;
    if(!lid) return;
    lastStatsQuery={type:'bbox',bounds:bounds};
    var el=document.getElementById('stats-results');
    document.getElementById('stats-loading').style.display='block';
    el.innerHTML='';
    var params='layer_id='+encodeURIComponent(lid)
        +'&south='+bounds.getSouth()+'&north='+bounds.getNorth()
        +'&west='+bounds.getWest()+'&east='+bounds.getEast();
    fetch('/api/stats?'+params)
        .then(function(r){return r.json()})
        .then(function(d){
            document.getElementById('stats-loading').style.display='none';
            if(d.error){el.innerHTML='<div style="color:#e74c3c">'+d.error+'</div>';return}
            renderStatsResults(d);
            updateStatsStepper();
        })
        .catch(function(){
            document.getElementById('stats-loading').style.display='none';
            el.innerHTML='<div style="color:#e74c3c">Request failed</div>';
        });
}
function fetchAreaStatsGeometry(geojson,layerIdOverride){
    var lid=layerIdOverride||activeOverlay.layerId;
    if(!lid) return;
    lastStatsQuery={type:'geometry',geojson:geojson};
    var el=document.getElementById('stats-results');
    document.getElementById('stats-loading').style.display='block';
    el.innerHTML='';
    fetch('/api/stats',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({layer_id:lid,geometry:geojson})
    })
        .then(function(r){return r.json()})
        .then(function(d){
            document.getElementById('stats-loading').style.display='none';
            if(d.error){el.innerHTML='<div style="color:#e74c3c">'+d.error+'</div>';return}
            renderStatsResults(d);
            updateStatsStepper();
        })
        .catch(function(){
            document.getElementById('stats-loading').style.display='none';
            el.innerHTML='<div style="color:#e74c3c">Request failed</div>';
        });
}
function fmtArea(km2){
    if(km2>=1) return km2.toLocaleString(undefined,{maximumFractionDigits:2})+' km\u00b2';
    return (km2*100).toFixed(2)+' ha';
}
function renderStatsResults(data){
    var el=document.getElementById('stats-results');
    var html='<div style="margin-bottom:6px;color:#aaa;font-size:.85em">Total area: '
        +fmtArea(data.total_area_km2)+'</div>';
    if(data.decimation_factor && data.decimation_factor>1){
        html+='<div style="color:#888;font-size:.75em;font-style:italic;margin-bottom:6px">'
            +'(estimated at 1/'+data.decimation_factor+'x resolution)</div>';
    }
    if(data.type==='categorical'){
        for(var i=0;i<data.classes.length;i++){
            var c=data.classes[i];
            html+='<div class="stats-row">'
                +'<div class="stats-swatch" style="background:'+c.color+'"></div>'
                +'<span style="flex:1">'+c.name+'</span>'
                +'<span style="text-align:right;white-space:nowrap">'+fmtArea(c.area_km2)+' ('+c.pct.toFixed(1)+'%)</span>'
                +'</div>'
                +'<div style="background:#2a2a4a;border-radius:2px;height:8px;margin:2px 0 4px">'
                +'<div class="stats-bar" style="width:'+c.pct+'%"></div></div>';
        }
    } else {
        html+='<div style="color:#aaa;font-size:.8em;margin-bottom:4px">'
            +'Range: '+data.min_val+' \u2013 '+data.max_val
            +' (mean: '+data.mean_val.toFixed(1)+')</div>';
        for(var i=0;i<data.buckets.length;i++){
            var b=data.buckets[i];
            html+='<div class="stats-row">'
                +'<div class="stats-swatch" style="background:'+b.color+'"></div>'
                +'<span style="flex:1">'+b.label+'</span>'
                +'<span style="text-align:right;white-space:nowrap">'+fmtArea(b.area_km2)+' ('+b.pct.toFixed(1)+'%)</span>'
                +'</div>'
                +'<div style="background:#2a2a4a;border-radius:2px;height:8px;margin:2px 0 4px">'
                +'<div class="stats-bar" style="width:'+b.pct+'%"></div></div>';
        }
    }
    el.innerHTML=html;
}

// =====================================================================
// Area Statistics — Time-Series Step
// =====================================================================
function updateStatsStepper(){
    var stepper=document.getElementById('stats-stepper');
    if(!activeOverlay||activeOverlay.datasetKey==='similarity'||!lastStatsQuery){
        stepper.style.display='none';
        return;
    }
    var years=DATASET_YEARS[activeOverlay.datasetKey];
    if(!years||years.length<2){
        stepper.style.display='none';
        return;
    }
    stepper.style.display='block';
    document.getElementById('stats-step-year').textContent=activeOverlay.year;
    var idx=years.indexOf(activeOverlay.year);
    document.getElementById('stats-step-back').disabled=(idx>=years.length-1);
    document.getElementById('stats-step-fwd').disabled=(idx<=0);
    document.getElementById('stats-step-range').textContent=years[years.length-1]+' \u2013 '+years[0];
}
function statsStepYear(direction){
    if(!activeOverlay||activeOverlay.datasetKey==='similarity'||!lastStatsQuery) return;
    var years=DATASET_YEARS[activeOverlay.datasetKey];
    if(!years) return;
    var idx=years.indexOf(activeOverlay.year);
    var newIdx=idx-direction;
    if(newIdx<0||newIdx>=years.length) return;
    // applyLayer picks up lastStatsQuery and refreshes stats automatically
    selectLayer(activeOverlay.datasetKey, years[newIdx]);
}

// =====================================================================
// draw:created event — routes to correct tool handler
// =====================================================================
map.on('draw:created', function(e){
    if(activeDrawMode==='stats'){
        statsRect=e.layer;
        drawnItems.addLayer(statsRect);
        document.getElementById('stats-draw-btn').classList.remove('active');
        document.getElementById('stats-clear-btn').style.display='inline-block';
        fetchAreaStats(statsRect.getBounds());
        activeDrawMode=null;
    } else if(activeDrawMode==='stats-poly'){
        statsRect=e.layer;
        drawnItems.addLayer(statsRect);
        document.getElementById('stats-poly-btn').classList.remove('active');
        document.getElementById('stats-clear-btn').style.display='inline-block';
        var latlngs=e.layer.getLatLngs()[0];
        var coords=latlngs.map(function(ll){return[parseFloat(ll.lng.toFixed(6)),parseFloat(ll.lat.toFixed(6))]});
        coords.push(coords[0]);
        var geojson={type:'Polygon',coordinates:[coords]};
        fetchAreaStatsGeometry(geojson);
        activeDrawMode=null;
    } else if(activeDrawMode==='geojson-point'||activeDrawMode==='geojson-rect'||activeDrawMode==='geojson-poly'){
        handleGeoJSONDrawn(e);
        activeDrawMode=null;
    }
});

// =====================================================================
// About the Datasets modal
// =====================================================================
function renderMd(md){
    var html=marked.parse(md);
    // Open external links in a new tab. Internal href="#" handlers (details icons) stay untouched.
    return html.replace(/<a\b([^>]*?)href="(https?:[^"]+)"([^>]*)>/g,function(m,before,url,after){
        if(/\btarget=/.test(m))return m;
        return '<a'+before+'href="'+url+'" target="_blank" rel="noopener noreferrer"'+after+'>';
    });
}
var _datasetsInfoHtml=null;
function showDatasetsInfo(){
    var modal=document.getElementById('info-modal');
    var body=document.getElementById('info-modal-body');
    modal.classList.add('show');
    if(_datasetsInfoHtml!==null){body.innerHTML=_datasetsInfoHtml;return;}
    fetch('/datasets.md',{cache:'no-cache'})
        .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.text();})
        .then(function(md){_datasetsInfoHtml=renderMd(md);body.innerHTML=_datasetsInfoHtml;})
        .catch(function(err){body.innerHTML='<p style="color:#e74c3c">Failed to load datasets.md: '+String(err).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];})+'</p>';});
}
function hideDatasetsInfo(event){
    if(event&&event.target&&event.target.id&&event.target.id!=='info-modal')return;
    document.getElementById('info-modal').classList.remove('show');
}
var _detailsHtmlCache={};
function showDetailsModal(mdPath){
    var modal=document.getElementById('details-modal');
    var body=document.getElementById('details-modal-body');
    modal.classList.add('show');
    if(_detailsHtmlCache[mdPath]){body.innerHTML=_detailsHtmlCache[mdPath];return;}
    body.innerHTML='Loading&hellip;';
    fetch(mdPath,{cache:'no-cache'})
        .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.text();})
        .then(function(md){_detailsHtmlCache[mdPath]=renderMd(md);body.innerHTML=_detailsHtmlCache[mdPath];})
        .catch(function(err){body.innerHTML='<p style="color:#e74c3c">Failed to load '+mdPath+': '+String(err).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];})+'</p>';});
}
function showGladDetails(){showDetailsModal('/glad_glclu.md');}
function showGlcDetails(){showDetailsModal('/glc_fcs30d.md');}
function showSunstoneDetails(){showDetailsModal('/sunstone_kenya_lulc_9C.md');}
function showDynamicWorldDetails(){showDetailsModal('/dynamicworld.md');}
function showEsriDetails(){showDetailsModal('/esri_lulc.md');}
function showWorldCoverDetails(){showDetailsModal('/worldcover.md');}
function showResolveEcoregionsDetails(){showDetailsModal('/resolve_ecoregions.md');}
function hideDetailsModal(event){
    if(event&&event.target&&event.target.id&&event.target.id!=='details-modal')return;
    document.getElementById('details-modal').classList.remove('show');
}
document.addEventListener('keydown',function(e){
    if(e.key==='Escape'){
        var d=document.getElementById('details-modal');
        if(d&&d.classList.contains('show')){d.classList.remove('show');return;}
        var m=document.getElementById('info-modal');
        if(m&&m.classList.contains('show'))m.classList.remove('show');
    }
});

// Make sure map fills its container
setTimeout(function(){map.invalidateSize()}, 200);
</script>
</body>
</html>"""

    # Substitute placeholders
    html = html.replace("__SIDEBAR_DATASETS__", sidebar_datasets)
    html = html.replace("__LEGEND_DATA__", json.dumps(legend_data))
    html = html.replace("__KENYA_CENTER__", json.dumps(KENYA_CENTER))
    html = html.replace("__KENYA_ZOOM__", str(KENYA_ZOOM))
    html = html.replace("__DATASET_YEARS__", json.dumps(dataset_years_map))
    return html


# =============================================================================
# HTTP Server
# =============================================================================

class MapHandler(http.server.SimpleHTTPRequestHandler):
    """Serves static files + /api/activate and /api/query endpoints."""

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/activate":
            self._handle_activate(parsed)
        elif parsed.path == "/api/query":
            self._handle_query(parsed)
        elif parsed.path == "/api/stats":
            self._handle_stats_bbox(parsed)
        elif parsed.path in (
            "/datasets.md", "/glad_glclu.md", "/glc_fcs30d.md",
            "/sunstone_kenya_lulc_9C.md", "/dynamicworld.md",
            "/esri_lulc.md", "/worldcover.md", "/resolve_ecoregions.md",
        ):
            self._handle_static_md(parsed.path.lstrip("/"))
        else:
            super().do_GET()

    def _handle_static_md(self, filename):
        md_path = Path(__file__).resolve().parent / "cloud" / "frontend" / filename
        try:
            body = md_path.read_bytes()
        except OSError as e:
            self.send_error(404, f"{filename} not found: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/stats":
            self._handle_stats_geometry()
        else:
            self.send_error(404)

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── /api/activate ───────────────────────────────────────────────────
    def _handle_activate(self, parsed):
        qs = parse_qs(parsed.query)
        layer_id = qs.get("layer_id", [None])[0]

        if not layer_id or layer_id not in _file_registry:
            self._send_json({"error": f"Unknown layer: {layer_id}"}, 404)
            return

        try:
            tile_url = get_tile_url(layer_id)
            if tile_url:
                self._send_json({"tile_url": tile_url, "layer_id": layer_id})
            else:
                self._send_json({"error": "Failed to create tile URL"}, 500)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    # ── /api/query ──────────────────────────────────────────────────────
    def _handle_query(self, parsed):
        qs = parse_qs(parsed.query)
        try:
            lat = float(qs["lat"][0])
            lng = float(qs["lng"][0])
            layer_id = qs["layer_id"][0]
        except (KeyError, ValueError, IndexError):
            self._send_json({"error": "Missing lat, lng, or layer_id"}, 400)
            return

        file_path = _file_registry.get(layer_id)
        if not file_path:
            self._send_json({"error": f"Unknown layer: {layer_id}"}, 404)
            return

        # Read pixel value with rasterio
        try:
            with rasterio.open(file_path) as src:
                row, col = rasterio.transform.rowcol(src.transform, lng, lat)
                if 0 <= row < src.height and 0 <= col < src.width:
                    val = int(src.read(
                        1, window=rasterio.windows.Window(col, row, 1, 1)
                    )[0, 0])
                else:
                    self._send_json({"error": "Outside raster bounds", "value": None})
                    return
        except Exception as e:
            self._send_json({"error": str(e)}, 500)
            return

        result = {"value": val, "lat": lat, "lng": lng}

        if layer_id == "similarity":
            # Continuous: compute color + description
            norm = min(1.0, max(0.0, val / 7200))
            if norm <= 0.5:
                t = norm * 2
                r, g = int(255 * t), 255
            else:
                t = (norm - 0.5) * 2
                r, g = 255, int(255 * (1 - t))
            result["color"] = f"#{r:02x}{g:02x}00"
            result["class_name"] = None
            if val < 500:
                result["description"] = "Very similar (minimal change)"
            elif val < 1500:
                result["description"] = "Similar (low change)"
            elif val < 3000:
                result["description"] = "Moderate change"
            elif val < 5000:
                result["description"] = "Significant change"
            else:
                result["description"] = "Very high change"
        else:
            # Categorical LULC
            dataset_key = resolve_dataset_key(layer_id)
            lookup = get_class_lookup(dataset_key)
            entry = lookup.get(int(val))
            if entry is not None:
                result["class_name"], result["color"] = entry
            else:
                result["class_name"] = f"Unknown ({val})"
                result["color"] = "#000000"

        self._send_json(result)

    # ── /api/stats (GET — bounding box) ──────────────────────────────
    def _handle_stats_bbox(self, parsed):
        """Compute class distribution within a bounding box."""
        qs = parse_qs(parsed.query)
        try:
            layer_id = qs["layer_id"][0]
            south = float(qs["south"][0])
            north = float(qs["north"][0])
            west = float(qs["west"][0])
            east = float(qs["east"][0])
        except (KeyError, ValueError, IndexError):
            self._send_json({"error": "Missing layer_id, south, north, west, or east"}, 400)
            return

        file_path = _file_registry.get(layer_id)
        if not file_path:
            self._send_json({"error": f"Unknown layer: {layer_id}"}, 404)
            return

        try:
            with rasterio.open(file_path) as src:
                row_n, col_w = rasterio.transform.rowcol(src.transform, west, north)
                row_s, col_e = rasterio.transform.rowcol(src.transform, east, south)

                # Ensure correct ordering
                r0, r1 = min(row_n, row_s), max(row_n, row_s)
                c0, c1 = min(col_w, col_e), max(col_w, col_e)

                # Clamp to raster bounds
                r0 = max(0, min(r0, src.height - 1))
                r1 = max(0, min(r1, src.height - 1))
                c0 = max(0, min(c0, src.width - 1))
                c1 = max(0, min(c1, src.width - 1))

                win_h = r1 - r0 + 1
                win_w = c1 - c0 + 1

                MAX_PIXELS = 1_000_000_000
                if win_h * win_w > MAX_PIXELS:
                    self._send_json({
                        "error": f"Selection too large ({win_h * win_w:,} pixels). "
                                 f"Max {MAX_PIXELS:,}. Zoom in or draw a smaller area."
                    }, 400)
                    return

                window = rasterio.windows.Window(c0, r0, win_w, win_h)

                # Decimate via COG overviews when window is large. Power-of-2 d
                # means out_shape lands on a pre-built overview level — no further
                # resampling, the baked-in overview data is returned directly.
                d = self._decimation_for(win_h, win_w)
                out_h, out_w = max(1, win_h // d), max(1, win_w // d)
                data = src.read(1, window=window, out_shape=(out_h, out_w))

                # Pixel area in km² from transform + center latitude, scaled up by d² since
                # each output pixel now covers d×d native pixels.
                center_lat = (south + north) / 2
                px_deg_x = abs(src.transform.a)
                px_deg_y = abs(src.transform.e)
                m_per_deg = 111_320 * math.cos(math.radians(center_lat))
                pixel_area_km2 = (px_deg_x * m_per_deg) * (px_deg_y * 111_320) / 1e6
                pixel_area_km2 *= d * d

        except Exception as e:
            self._send_json({"error": f"Raster read error: {e}"}, 500)
            return

        if layer_id == "similarity":
            result = self._stats_continuous(data, pixel_area_km2, decimation_factor=d)
        else:
            dataset_key = resolve_dataset_key(layer_id)
            result = self._stats_categorical(data, dataset_key, pixel_area_km2, decimation_factor=d)

        self._send_json(result)

    # ── /api/stats (POST — GeoJSON geometry) ───────────────────────────
    def _handle_stats_geometry(self):
        """Compute class distribution within an arbitrary GeoJSON geometry."""
        from shapely.geometry import shape, mapping

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            layer_id = body["layer_id"]
            geometry = body["geometry"]
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            self._send_json({"error": f"Invalid request body: {e}"}, 400)
            return

        file_path = _file_registry.get(layer_id)
        if not file_path:
            self._send_json({"error": f"Unknown layer: {layer_id}"}, 404)
            return

        # Normalize to a list of shapely geometries
        try:
            if geometry.get("type") in ("FeatureCollection",):
                shapes = [shape(f["geometry"]) for f in geometry["features"]]
            elif geometry.get("type") == "Feature":
                shapes = [shape(geometry["geometry"])]
            else:
                shapes = [shape(geometry)]
            geom_jsons = [mapping(s) for s in shapes]
        except Exception as e:
            self._send_json({"error": f"Invalid geometry: {e}"}, 400)
            return

        try:
            with rasterio.open(file_path) as src:
                # Find the raster window covering the geometry's native bbox.
                window = geometry_window(src, geom_jsons).round_offsets().round_shape()
                native_h, native_w = int(window.height), int(window.width)

                # Decimate via COG overviews when the window is large. Power-of-2 d
                # means out_shape lands on a pre-built overview level — no further
                # resampling needed.
                d = self._decimation_for(native_h, native_w)
                out_h = max(1, native_h // d)
                out_w = max(1, native_w // d)

                data = src.read(1, window=window, out_shape=(out_h, out_w))

                # Rasterize polygon onto the same decimated grid and KEEP ONLY
                # pixels inside. Crucial: we cannot fill outside pixels with a
                # sentinel (like 0), because for categorical rasters 0 is often
                # a real class ("Bare Ground" in Sunstone) — those sentinel pixels
                # would inflate class counts and the reported total area.
                win_tf = src.window_transform(window)
                dec_tf = Affine(win_tf.a * d, 0, win_tf.c,
                                0, win_tf.e * d, win_tf.f)
                mask = geometry_mask(geom_jsons, out_shape=(out_h, out_w),
                                     transform=dec_tf, invert=True)
                data = data[mask]  # flat 1D array of inside-polygon values

                # Pixel area in km² — scale up by d² since each output pixel now
                # covers d×d native pixels.
                center_lat = sum(s.centroid.y for s in shapes) / len(shapes)
                px_deg_x = abs(src.transform.a)
                px_deg_y = abs(src.transform.e)
                m_per_deg = 111_320 * math.cos(math.radians(center_lat))
                pixel_area_km2 = (px_deg_x * m_per_deg) * (px_deg_y * 111_320) / 1e6
                pixel_area_km2 *= d * d

        except Exception as e:
            self._send_json({"error": f"Raster read error: {e}"}, 500)
            return

        if layer_id == "similarity":
            result = self._stats_continuous(data, pixel_area_km2, decimation_factor=d)
        else:
            dataset_key = resolve_dataset_key(layer_id)
            result = self._stats_categorical(data, dataset_key, pixel_area_km2, decimation_factor=d)

        self._send_json(result)

    @staticmethod
    def _decimation_for(native_h, native_w, threshold=10_000_000, max_side=3163):
        """Pick a decimation factor for a raster window.

        Returns 1 when native pixel count <= threshold (full-resolution read).
        Otherwise snaps to the next power of 2 so `out_shape=(h//d, w//d)` lands
        exactly on a COG overview level (typically 2x, 4x, 8x, 16x), letting
        rasterio read the pre-downsampled data directly with no further resampling.
        """
        if native_h * native_w <= threshold:
            return 1
        d_raw = max(2, math.ceil(max(native_h, native_w) / max_side))
        # Round up to the next power of 2: 3→4, 5→8, 9→16, etc.
        return 1 << (d_raw - 1).bit_length()

    def _stats_categorical(self, data, dataset_key, pixel_area_km2, decimation_factor=1):
        """Compute class distribution for categorical LULC data."""
        lookup = get_class_lookup(dataset_key)
        unique, counts = np.unique(data, return_counts=True)
        total = int(counts.sum())

        # Aggregate pixel counts by display name, since grouped datasets map
        # multiple raw values to the same group.
        merged = {}
        for val, count in zip(unique, counts):
            val, count = int(val), int(count)
            entry = lookup.get(val)
            if entry is None:
                name, color = f"Unknown ({val})", "#000000"
            else:
                name, color = entry

            if name == "No Data":
                total -= count
                continue

            existing = merged.get(name)
            if existing is None:
                merged[name] = {"value": val, "name": name, "color": color, "count": count}
            else:
                existing["count"] += count

        classes = sorted(merged.values(), key=lambda x: -x["count"])

        for c in classes:
            c["pct"] = (c["count"] / total * 100) if total > 0 else 0
            c["area_km2"] = round(c["count"] * pixel_area_km2, 2)

        total_area_km2 = round(total * pixel_area_km2, 2)
        return {"type": "categorical", "total_pixels": total,
                "total_area_km2": total_area_km2, "classes": classes,
                "decimation_factor": decimation_factor}

    def _stats_continuous(self, data, pixel_area_km2, decimation_factor=1):
        """Compute value distribution for continuous (similarity) data."""
        valid = data[data > 0]
        total = int(valid.size)
        if total == 0:
            return {"type": "continuous", "total_pixels": 0, "total_area_km2": 0,
                    "buckets": [], "min_val": 0, "max_val": 0, "mean_val": 0,
                    "decimation_factor": decimation_factor}

        bucket_defs = [
            (0, 500, "Very similar (minimal change)", "#00ff00"),
            (500, 1500, "Similar (low change)", "#7fff00"),
            (1500, 3000, "Moderate change", "#ffff00"),
            (3000, 5000, "Significant change", "#ff7f00"),
            (5000, 10000, "Very high change", "#ff0000"),
        ]
        buckets = []
        for lo, hi, label, color in bucket_defs:
            count = int(np.sum((valid >= lo) & (valid < hi)))
            if count > 0:
                buckets.append({"label": label, "color": color, "count": count,
                                "pct": count / total * 100,
                                "area_km2": round(count * pixel_area_km2, 2)})

        total_area_km2 = round(total * pixel_area_km2, 2)
        return {
            "type": "continuous", "total_pixels": total,
            "total_area_km2": total_area_km2,
            "min_val": int(valid.min()), "max_val": int(valid.max()),
            "mean_val": float(valid.mean()), "buckets": buckets,
            "decimation_factor": decimation_factor,
        }

    def log_message(self, fmt, *args):
        # Only log API requests, suppress static file noise
        msg = args[0] if args else ""
        if "/api/" in str(msg):
            super().log_message(fmt, *args)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


# =============================================================================
# Main
# =============================================================================

def main():
    global _file_registry, _label_mappings, _external_ip, _next_tile_port

    parser = argparse.ArgumentParser(description="KenyaMap — Kenya Land Cover Viewer")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--external-ip", type=str, default=None,
                        help="External IP for tile URLs (required for GCP deployment)")
    parser.add_argument("--tile-port-start", type=int, default=DEFAULT_TILE_PORT_START,
                        help="Starting port for tile servers (default: 8001)")
    args = parser.parse_args()

    _external_ip = args.external_ip
    _next_tile_port = args.tile_port_start

    print("KenyaMap — Starting up...")

    # Validate paths
    if not DATA_DIR.exists():
        print(f"Error: data directory '{DATA_DIR}' not found."); sys.exit(1)
    if not MAPPING_FILE.exists():
        print(f"Error: '{MAPPING_FILE}' not found."); sys.exit(1)

    # Load label mappings
    print("Loading dataset label mappings...")
    with open(MAPPING_FILE) as f:
        _label_mappings = json.load(f)

    # Discover available files
    print("Scanning data directory...")
    _file_registry, datasets = discover_files()
    print(f"  Found {len(_file_registry)} available layers")

    if not _file_registry:
        print("Error: no data files found in data/"); sys.exit(1)

    # Build legend data
    legend_data = build_legend_data()

    # Generate HTML
    print("Generating map HTML...")
    html = generate_html(datasets, legend_data)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"  Saved to {OUTPUT_HTML}")

    # Start HTTP server
    bind_addr = "0.0.0.0" if _external_ip else "127.0.0.1"
    print(f"\nStarting HTTP server on {bind_addr}:{args.port}...")
    server = ThreadedHTTPServer((bind_addr, args.port), MapHandler)

    host = _external_ip or "localhost"
    url = f"http://{host}:{args.port}/{OUTPUT_HTML}"
    print(f"  Open in browser: {url}\n")

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()

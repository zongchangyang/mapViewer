"""
KenyaMap — Kenya Land Cover & Similarity Map Viewer

Serves Cloud Optimized GeoTIFF (COG) raster data over an interactive Leaflet.js map.
Supports multiple LULC datasets (2017-2025) and a similarity change detection layer.

Usage:
    python app.py [--port PORT] [--no-browser]
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

import numpy as np
import rasterio
from localtileserver import TileClient

# =============================================================================
# Configuration
# =============================================================================

DATA_DIR = Path("data")
MAPPING_FILE = Path("dataset_label_mapping.json")
OUTPUT_HTML = Path("map.html")
KENYA_CENTER = [0.0236, 37.9062]
KENYA_ZOOM = 7
DEFAULT_PORT = 8000

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
        "available_years": [2020],
    },
    "glc_fcs30d": {
        "display_name": "GLC_FCS30D",
        "file_template": "glc_fcs30d_{year}_cog.tif",
        "available_years": [2021, 2020, 2019],
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


def build_colormap_dict(dataset_key):
    """Build sparse {pixel_value: [R,G,B,A]} dict for a categorical dataset."""
    if dataset_key in _colormap_cache:
        return _colormap_cache[dataset_key]

    ds = _label_mappings[dataset_key]
    cmap = {}

    if "simplified_groups" in ds:
        sg = ds["simplified_groups"]
        for orig_str, group_idx in sg["group_mapping"].items():
            orig = int(orig_str)
            if 0 <= orig <= 255:
                rgb = hex_to_rgb(sg["group_colors"][group_idx])
                cmap[orig] = rgb + [255]
    else:
        for orig_str, remapped_idx in ds["label_mapping"].items():
            orig = int(orig_str)
            if 0 <= orig <= 255 and remapped_idx < len(ds["palette"]):
                rgb = hex_to_rgb(ds["palette"][remapped_idx])
                cmap[orig] = rgb + [255]

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
    if layer_id not in _file_registry:
        return None

    if layer_id not in _tile_clients:
        print(f"  Starting tile server for {layer_id}...")
        _tile_clients[layer_id] = TileClient(_file_registry[layer_id], cors_all=True)

    client = _tile_clients[layer_id]

    if layer_id == "similarity":
        # Continuous green → yellow → red
        return client.get_tile_url(
            indexes=[1],
            colormap='rdylgn_r',
            vmin=0,
            vmax=7200,
        )

    # Categorical LULC — build colormap dict, append to URL
    dataset_key = resolve_dataset_key(layer_id)
    cmap_dict = build_colormap_dict(dataset_key)

    # vmin=0, vmax=255 prevents auto-scaling on UInt8 data
    base_url = client.get_tile_url(indexes=[1], vmin=0, vmax=255)
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

        if "simplified_groups" in ds:
            sg = ds["simplified_groups"]
            entries = [
                {"name": n, "color": c}
                for n, c in zip(sg["group_names"], sg["group_colors"])
                if n != "No Data"
            ]
        else:
            entries = []
            for name, color in zip(ds["class_names"], ds["palette"]):
                if name == "No Data":
                    continue
                entries.append({"name": name, "color": color})

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
  border-radius:4px;cursor:pointer;font-size:.8em;transition:background .2s;
}
.tool-btn:hover{background:#1a5276}
.tool-btn.active{background:#00d2ff;color:#1a1a2e}
.tool-btn:disabled{opacity:.4;cursor:not-allowed}
.stats-results{margin-top:10px;font-size:.8em}
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
.year-stepper{display:flex;align-items:center;gap:10px;margin-top:8px}
.year-stepper .year-display{
  flex:1;text-align:center;font-size:1.2em;color:#00d2ff;font-weight:600;
}
.step-btn{
  width:36px;height:36px;background:#0f3460;color:#00d2ff;
  border:1px solid #00d2ff;border-radius:50%;cursor:pointer;
  font-size:1.1em;display:flex;align-items:center;justify-content:center;
}
.step-btn:hover{background:#1a5276}
.step-btn:disabled{opacity:.3;cursor:not-allowed}
.dataset-indicator{font-size:.8em;color:#888;margin-top:6px;text-align:center}
.no-layer-msg{color:#666;font-size:.8em;font-style:italic}

/* ── Map ────────────────────────────────────────────────────────────── */
#map{flex:1;z-index:1}

/* ── Legend ──────────────────────────────────────────────────────────── */
.legend-control{
  background:rgba(255,255,255,.92);padding:10px 14px;border-radius:8px;
  box-shadow:0 2px 8px rgba(0,0,0,.3);max-height:400px;overflow-y:auto;
  min-width:160px;
}
.legend-control h4{margin:0 0 8px;font-size:.85em;color:#333}
.legend-entry{display:flex;align-items:center;margin:3px 0;font-size:.8em;color:#444}
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
    <h3>Basemap</h3>
    <div class="basemap-option" id="bm-osm" onclick="setBasemap('osm')">
      OpenStreetMap
    </div>
    <div class="basemap-option active" id="bm-esri" onclick="setBasemap('esri')">
      ESRI World Imagery
    </div>
  </div>

  <div class="sidebar-section">
    <h3>LULC Layers</h3>
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
      <input type="file" id="geojson-file-input" accept=".json,.geojson,.zip,.shp" style="display:none" onchange="handleFileUpload(this)">
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
        <input type="file" id="stats-file-input" accept=".json,.geojson,.zip,.shp" style="display:none" onchange="handleStatsFileUpload(this)">
        <button class="tool-btn" id="stats-clear-btn" onclick="clearStatsRect()" style="display:none">Clear</button>
        <div id="stats-loading" style="display:none;color:#888;font-size:.8em;margin-top:8px">
          Computing statistics...
        </div>
        <div id="stats-results" class="stats-results"></div>
      </div>
    </div>
  </div>

  <!-- Tool 3: Time-series Stepper -->
  <div class="tool-section">
    <div class="tool-section-header" onclick="toggleToolSection(this)">
      <span class="arrow">&#9656;</span> Time-series Step
    </div>
    <div class="tool-section-body">
      <div id="stepper-no-layer" class="no-layer-msg">Select a dataset layer first</div>
      <div id="stepper-controls" style="display:none">
        <div class="dataset-indicator" id="stepper-dataset-name"></div>
        <div class="year-stepper">
          <button class="step-btn" id="stepper-back" onclick="stepYear(-1)">&#9664;</button>
          <div class="year-display" id="stepper-year">----</div>
          <button class="step-btn" id="stepper-fwd" onclick="stepYear(1)">&#9654;</button>
        </div>
        <div class="dataset-indicator" id="stepper-range"></div>
      </div>
    </div>
  </div>
</div>

</div><!-- /container -->

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
    updateStepperState();
    updateStatsVisibility();
    prefetchAdjacentYears();
}

function clearActiveLayer(){
    if(activeOverlay && activeOverlay.tileLayer) map.removeLayer(activeOverlay.tileLayer);
    activeOverlay = null;
    updateLegend(null);
    updateSidebarHighlight(null);
    document.getElementById('clear-btn').classList.remove('show');
    updateStepperState();
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
map.on('click', function(e){
    if(activeDrawMode) return;
    if(!activeOverlay) return;
    var lat = e.latlng.lat.toFixed(6), lng = e.latlng.lng.toFixed(6);
    fetch('/api/query?lat='+lat+'&lng='+lng+'&layer_id='+encodeURIComponent(activeOverlay.layerId))
        .then(function(r){return r.json()})
        .then(function(d){
            if(d.error) return;
            var c = '<div class="pixel-info">';
            if(d.class_name){
                c += '<div><span class="pi-swatch" style="background:'+d.color+'"></span>';
                c += '<span class="pi-class">'+d.class_name+'</span></div>';
            } else if(d.description){
                c += '<div><span class="pi-swatch" style="background:'+d.color+'"></span>';
                c += '<span class="pi-class">'+d.description+'</span></div>';
            }
            c += '<div>Pixel value: '+d.value+'</div>';
            c += '<div style="color:#888;font-size:11px">'+lat+', '+lng+'</div></div>';
            L.popup().setLatLng(e.latlng).setContent(c).openOn(map);
        })
        .catch(function(){});
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
function handleFileUpload(input){
    if(!input.files||!input.files[0]) return;
    var file=input.files[0];
    var name=file.name.toLowerCase();
    if(name.endsWith('.zip')||name.endsWith('.shp')){
        var reader=new FileReader();
        reader.onload=function(e){
            shp(e.target.result).then(function(geojson){
                displayGeoJSON(geojson);
            }).catch(function(err){alert('Failed to parse shapefile: '+err)});
        };
        reader.readAsArrayBuffer(file);
    } else {
        var reader=new FileReader();
        reader.onload=function(e){
            try{
                var geojson=JSON.parse(e.target.result);
                displayGeoJSON(geojson);
            }catch(err){alert('Failed to parse GeoJSON: '+err)}
        };
        reader.readAsText(file);
    }
    input.value='';
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
// ToolBox — Time-series Stepper
// =====================================================================
function updateStepperState(){
    var noLayer=document.getElementById('stepper-no-layer');
    var controls=document.getElementById('stepper-controls');
    if(!activeOverlay || activeOverlay.datasetKey==='similarity'){
        noLayer.style.display='block';
        noLayer.textContent=activeOverlay?'Similarity layer has no year series':'Select a dataset layer first';
        controls.style.display='none';
        return;
    }
    var dsKey=activeOverlay.datasetKey;
    var years=DATASET_YEARS[dsKey];
    if(!years||years.length<2){
        noLayer.style.display='block';
        noLayer.textContent='Only one year available for this dataset';
        controls.style.display='none';
        return;
    }
    noLayer.style.display='none';
    controls.style.display='block';
    document.getElementById('stepper-dataset-name').textContent=dsKey.replace(/_/g,' ');
    document.getElementById('stepper-year').textContent=activeOverlay.year;
    var idx=years.indexOf(activeOverlay.year);
    document.getElementById('stepper-fwd').disabled=(idx<=0);
    document.getElementById('stepper-back').disabled=(idx>=years.length-1);
    document.getElementById('stepper-range').textContent=years[years.length-1]+' \u2013 '+years[0];
}
function stepYear(direction){
    if(!activeOverlay||activeOverlay.datasetKey==='similarity') return;
    var years=DATASET_YEARS[activeOverlay.datasetKey];
    if(!years) return;
    var idx=years.indexOf(activeOverlay.year);
    var newIdx=idx-direction;
    if(newIdx<0||newIdx>=years.length) return;
    selectLayer(activeOverlay.datasetKey,years[newIdx]);
}
function prefetchAdjacentYears(){
    if(!activeOverlay||activeOverlay.datasetKey==='similarity') return;
    var years=DATASET_YEARS[activeOverlay.datasetKey];
    if(!years) return;
    var idx=years.indexOf(activeOverlay.year);
    var toFetch=[];
    if(idx>0) toFetch.push(years[idx-1]);
    if(idx<years.length-1) toFetch.push(years[idx+1]);
    toFetch.forEach(function(y){
        var lid=activeOverlay.datasetKey+'_'+y;
        if(!tileUrlCache[lid]){
            fetch('/api/activate?layer_id='+encodeURIComponent(lid))
                .then(function(r){return r.json()})
                .then(function(d){if(d.tile_url) tileUrlCache[lid]=d.tile_url});
        }
    });
}

// =====================================================================
// ToolBox — Area Statistics
// =====================================================================
var statsRect = null;

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
}
function handleStatsFileUpload(input){
    if(!input.files||!input.files[0]||!activeOverlay) return;
    var file=input.files[0];
    var name=file.name.toLowerCase();
    if(name.endsWith('.zip')||name.endsWith('.shp')){
        var reader=new FileReader();
        reader.onload=function(e){
            shp(e.target.result).then(function(geojson){
                applyStatsGeometry(geojson);
            }).catch(function(err){alert('Failed to parse shapefile: '+err)});
        };
        reader.readAsArrayBuffer(file);
    } else {
        var reader=new FileReader();
        reader.onload=function(e){
            try{
                var geojson=JSON.parse(e.target.result);
                applyStatsGeometry(geojson);
            }catch(err){alert('Failed to parse GeoJSON: '+err)}
        };
        reader.readAsText(file);
    }
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
function fetchAreaStats(bounds){
    if(!activeOverlay) return;
    var el=document.getElementById('stats-results');
    document.getElementById('stats-loading').style.display='block';
    el.innerHTML='';
    var params='layer_id='+encodeURIComponent(activeOverlay.layerId)
        +'&south='+bounds.getSouth()+'&north='+bounds.getNorth()
        +'&west='+bounds.getWest()+'&east='+bounds.getEast();
    fetch('/api/stats?'+params)
        .then(function(r){return r.json()})
        .then(function(d){
            document.getElementById('stats-loading').style.display='none';
            if(d.error){el.innerHTML='<div style="color:#e74c3c">'+d.error+'</div>';return}
            renderStatsResults(d);
        })
        .catch(function(){
            document.getElementById('stats-loading').style.display='none';
            el.innerHTML='<div style="color:#e74c3c">Request failed</div>';
        });
}
function fetchAreaStatsGeometry(geojson){
    if(!activeOverlay) return;
    var el=document.getElementById('stats-results');
    document.getElementById('stats-loading').style.display='block';
    el.innerHTML='';
    fetch('/api/stats',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({layer_id:activeOverlay.layerId,geometry:geojson})
    })
        .then(function(r){return r.json()})
        .then(function(d){
            document.getElementById('stats-loading').style.display='none';
            if(d.error){el.innerHTML='<div style="color:#e74c3c">'+d.error+'</div>';return}
            renderStatsResults(d);
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
        else:
            super().do_GET()

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
            ds = _label_mappings.get(dataset_key, {})

            if "simplified_groups" in ds:
                sg = ds["simplified_groups"]
                gidx = sg["group_mapping"].get(str(val))
                if gidx is not None:
                    result["class_name"] = sg["group_names"][gidx]
                    result["color"] = sg["group_colors"][gidx]
                else:
                    result["class_name"] = f"Unknown ({val})"
                    result["color"] = "#000000"
            elif "label_mapping" in ds:
                idx = ds["label_mapping"].get(str(val))
                if idx is not None and idx < len(ds["class_names"]):
                    result["class_name"] = ds["class_names"][idx]
                    result["color"] = (
                        ds["palette"][idx] if idx < len(ds["palette"]) else "#000000"
                    )
                else:
                    result["class_name"] = f"Unknown ({val})"
                    result["color"] = "#000000"
            else:
                result["class_name"] = str(val)
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

                MAX_PIXELS = 100_000_000
                if win_h * win_w > MAX_PIXELS:
                    self._send_json({
                        "error": f"Selection too large ({win_h * win_w:,} pixels). "
                                 f"Max {MAX_PIXELS:,}. Zoom in or draw a smaller area."
                    }, 400)
                    return

                window = rasterio.windows.Window(c0, r0, win_w, win_h)
                data = src.read(1, window=window)

                # Compute pixel area in km² from transform + center latitude
                import math
                center_lat = (south + north) / 2
                px_deg_x = abs(src.transform.a)
                px_deg_y = abs(src.transform.e)
                m_per_deg = 111_320 * math.cos(math.radians(center_lat))
                pixel_area_km2 = (px_deg_x * m_per_deg) * (px_deg_y * 111_320) / 1e6

        except Exception as e:
            self._send_json({"error": f"Raster read error: {e}"}, 500)
            return

        if layer_id == "similarity":
            result = self._stats_continuous(data, pixel_area_km2)
        else:
            dataset_key = resolve_dataset_key(layer_id)
            result = self._stats_categorical(data, dataset_key, pixel_area_km2)

        self._send_json(result)

    # ── /api/stats (POST — GeoJSON geometry) ───────────────────────────
    def _handle_stats_geometry(self):
        """Compute class distribution within an arbitrary GeoJSON geometry."""
        import math
        from rasterio.mask import mask as rasterio_mask
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
                data, _ = rasterio_mask(src, geom_jsons, crop=True,
                                        nodata=0, filled=True)
                data = data[0]  # single band

                # Pixel area
                center_lat = sum(s.centroid.y for s in shapes) / len(shapes)
                px_deg_x = abs(src.transform.a)
                px_deg_y = abs(src.transform.e)
                m_per_deg = 111_320 * math.cos(math.radians(center_lat))
                pixel_area_km2 = (px_deg_x * m_per_deg) * (px_deg_y * 111_320) / 1e6

        except Exception as e:
            self._send_json({"error": f"Raster read error: {e}"}, 500)
            return

        if layer_id == "similarity":
            result = self._stats_continuous(data, pixel_area_km2)
        else:
            dataset_key = resolve_dataset_key(layer_id)
            result = self._stats_categorical(data, dataset_key, pixel_area_km2)

        self._send_json(result)

    def _stats_categorical(self, data, dataset_key, pixel_area_km2):
        """Compute class distribution for categorical LULC data."""
        ds = _label_mappings.get(dataset_key, {})
        unique, counts = np.unique(data, return_counts=True)
        total = int(counts.sum())

        classes = []
        for val, count in sorted(zip(unique, counts), key=lambda x: -x[1]):
            val, count = int(val), int(count)

            if "simplified_groups" in ds:
                sg = ds["simplified_groups"]
                gidx = sg["group_mapping"].get(str(val))
                if gidx is not None:
                    name = sg["group_names"][gidx]
                    color = sg["group_colors"][gidx]
                else:
                    name, color = f"Unknown ({val})", "#000000"
            elif "label_mapping" in ds:
                idx = ds["label_mapping"].get(str(val))
                if idx is not None and idx < len(ds.get("class_names", [])):
                    name = ds["class_names"][idx]
                    color = ds["palette"][idx] if idx < len(ds.get("palette", [])) else "#000000"
                else:
                    name, color = f"Unknown ({val})", "#000000"
            else:
                name, color = str(val), "#000000"

            if name == "No Data":
                total -= count
                continue
            classes.append({"value": val, "name": name, "color": color, "count": count})

        # Merge duplicate group names (simplified_groups maps multiple pixel values to one group)
        merged = {}
        for c in classes:
            if c["name"] in merged:
                merged[c["name"]]["count"] += c["count"]
            else:
                merged[c["name"]] = dict(c)
        classes = sorted(merged.values(), key=lambda x: -x["count"])

        for c in classes:
            c["pct"] = (c["count"] / total * 100) if total > 0 else 0
            c["area_km2"] = round(c["count"] * pixel_area_km2, 2)

        total_area_km2 = round(total * pixel_area_km2, 2)
        return {"type": "categorical", "total_pixels": total,
                "total_area_km2": total_area_km2, "classes": classes}

    def _stats_continuous(self, data, pixel_area_km2):
        """Compute value distribution for continuous (similarity) data."""
        valid = data[data > 0]
        total = int(valid.size)
        if total == 0:
            return {"type": "continuous", "total_pixels": 0, "total_area_km2": 0,
                    "buckets": [], "min_val": 0, "max_val": 0, "mean_val": 0}

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
    global _file_registry, _label_mappings

    parser = argparse.ArgumentParser(description="KenyaMap — Kenya Land Cover Viewer")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

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
    print(f"\nStarting HTTP server on port {args.port}...")
    server = ThreadedHTTPServer(("", args.port), MapHandler)

    url = f"http://localhost:{args.port}/{OUTPUT_HTML}"
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

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

import rasterio
from localtileserver import TileClient

# =============================================================================
# Configuration
# =============================================================================

DATA_DIR = Path("data")
MAPPING_FILE = Path("dataset_label_mapping.json")
OUTPUT_HTML = Path("map.html")
KENYA_CENTER = [0.0236, 37.9062]
KENYA_ZOOM = 6
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
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif}
.container{display:flex;height:100vh}

/* ── Sidebar ────────────────────────────────────────────────────────── */
#sidebar{
  width:300px;flex-shrink:0;background:#1a1a2e;color:#e0e0e0;
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

</div><!-- /container -->

<script>
// =====================================================================
// Configuration (injected by Python)
// =====================================================================
var LEGEND_DATA = __LEGEND_DATA__;

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
}

function clearActiveLayer(){
    if(activeOverlay && activeOverlay.tileLayer) map.removeLayer(activeOverlay.tileLayer);
    activeOverlay = null;
    updateLegend(null);
    updateSidebarHighlight(null);
    document.getElementById('clear-btn').classList.remove('show');
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
        else:
            super().do_GET()

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

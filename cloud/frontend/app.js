// KenyaMap cloud frontend — talks directly to TiTiler for tiles, point queries,
// and zonal statistics. Class names / colors are decoded client-side from
// dataset_label_mapping.json.

var CFG = window.KENYAMAP_CONFIG;
var TITILER_BASE = CFG.TITILER_BASE;

// =====================================================================
// State
// =====================================================================
var LABEL_MAPPING = null;   // dataset_label_mapping.json
var LAYER_INDEX   = {};     // layer_id -> {dataset_key, year, url, display_name}
var DATASETS      = [];     // ordered list of {key, display_name, years:[{year, layer_id}]}
var DATASET_YEARS = {};     // dataset_key -> [year, year, ...] (desc)
var LEGEND_DATA   = {};     // dataset_key -> {title, entries|colors+min_label+max_label, type?}
var _colormap_cache = {};

var activeOverlay  = null;   // {datasetKey, year, layerId, tileLayer}
var currentOpacity = 0.8;
var currentBasemap = 'esri';
var activeDrawMode = null;
var geojsonLayer   = null;
var statsRect      = null;
var lastStatsQuery = null;   // {type:'bbox',bounds} | {type:'geometry',geojson}

// =====================================================================
// Map init
// =====================================================================
var map = L.map('map', {
    center: CFG.KENYA_CENTER,
    zoom: CFG.KENYA_ZOOM,
    zoomControl: true,
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
};
basemaps.esri.addTo(map);

var drawnItems = new L.FeatureGroup();
map.addLayer(drawnItems);

var legendControl = L.control({position:'topright'});
legendControl.onAdd = function(){
    this._div = L.DomUtil.create('div','legend-control');
    this._div.style.display = 'none';
    L.DomEvent.disableClickPropagation(this._div);
    L.DomEvent.disableScrollPropagation(this._div);
    return this._div;
};
legendControl.addTo(map);

// =====================================================================
// Bootstrap: load label mapping + layer index, build sidebar/legend
// =====================================================================
async function bootstrap(){
    var [lm, layersResp] = await Promise.all([
        fetch('dataset_label_mapping.json').then(function(r){return r.json()}),
        fetch(TITILER_BASE + '/layers').then(function(r){return r.json()}),
    ]);
    LABEL_MAPPING = lm;

    // Group layers by dataset_key, sort years descending
    var groups = {};
    for (var i=0; i<layersResp.layers.length; i++) {
        var l = layersResp.layers[i];
        LAYER_INDEX[l.layer_id] = l;
        if (l.dataset_key === 'similarity') continue;
        if (!groups[l.dataset_key]) {
            groups[l.dataset_key] = {
                key: l.dataset_key,
                display_name: l.display_name,
                years: [],
            };
        }
        groups[l.dataset_key].years.push({year: l.year, layer_id: l.layer_id});
    }
    // Preserve backend order via DATASET_REGISTRY key order (server returns insertion-order)
    var seen = {};
    for (var i=0; i<layersResp.layers.length; i++) {
        var key = layersResp.layers[i].dataset_key;
        if (key === 'similarity' || seen[key]) continue;
        seen[key] = true;
        groups[key].years.sort(function(a,b){return b.year - a.year});
        DATASETS.push(groups[key]);
        DATASET_YEARS[key] = groups[key].years.map(function(y){return y.year});
    }

    LEGEND_DATA = buildLegendData();
    renderSidebarDatasets();
}

// =====================================================================
// Helpers: color / colormap / dataset key
// =====================================================================
function hexToRgb(hex){
    var h = hex.replace('#','');
    return [
        parseInt(h.substr(0,2),16),
        parseInt(h.substr(2,2),16),
        parseInt(h.substr(4,2),16),
    ];
}

function resolveDatasetKey(layerId){
    if (layerId === 'similarity') return 'similarity';
    var entry = LAYER_INDEX[layerId];
    return entry ? entry.dataset_key : layerId;
}

// Sparse {pixel_value: [R,G,B,A]} — matches app.py build_colormap_dict
function buildColormap(datasetKey){
    if (_colormap_cache[datasetKey]) return _colormap_cache[datasetKey];
    var ds = LABEL_MAPPING[datasetKey];
    if (!ds) return null;
    var cmap = {};
    if (ds.simplified_groups) {
        var sg = ds.simplified_groups;
        for (var origStr in sg.group_mapping) {
            var orig = parseInt(origStr, 10);
            if (orig < 0 || orig > 255) continue;
            var rgb = hexToRgb(sg.group_colors[sg.group_mapping[origStr]]);
            cmap[orig] = [rgb[0], rgb[1], rgb[2], 255];
        }
    } else {
        for (var origStr in ds.label_mapping) {
            var orig = parseInt(origStr, 10);
            var remappedIdx = ds.label_mapping[origStr];
            if (orig < 0 || orig > 255 || remappedIdx >= ds.palette.length) continue;
            var rgb = hexToRgb(ds.palette[remappedIdx]);
            cmap[orig] = [rgb[0], rgb[1], rgb[2], 255];
        }
    }
    _colormap_cache[datasetKey] = cmap;
    return cmap;
}

function buildLegendData(){
    var out = {};
    for (var key in LABEL_MAPPING) {
        if (key.charAt(0) === '_') continue;
        var ds = LABEL_MAPPING[key];
        var entries = [];
        if (ds.simplified_groups) {
            var sg = ds.simplified_groups;
            for (var i=0; i<sg.group_names.length; i++) {
                if (sg.group_names[i] === 'No Data') continue;
                entries.push({name: sg.group_names[i], color: sg.group_colors[i]});
            }
        } else if (ds.class_names) {
            for (var i=0; i<ds.class_names.length; i++) {
                if (ds.class_names[i] === 'No Data') continue;
                entries.push({name: ds.class_names[i], color: ds.palette[i]});
            }
        }
        out[key] = {title: ds.name || key, entries: entries};
    }
    out.similarity = {
        title: 'Land Change Similarity (2017 vs 2025)',
        type: 'continuous',
        min_label: 'No Change',
        max_label: 'High Change',
        colors: ['#00ff00','#ffff00','#ff0000'],
    };
    return out;
}

// Decode a single pixel value → {class_name, color, description?}
function decodePixelValue(value, layerId){
    if (layerId === 'similarity') {
        var norm = Math.min(1, Math.max(0, value/7200));
        var r, g;
        if (norm <= 0.5) { var t = norm*2; r = Math.round(255*t); g = 255; }
        else             { var t = (norm-0.5)*2; r = 255; g = Math.round(255*(1-t)); }
        var color = '#'+((r<<16)|(g<<8)).toString(16).padStart(6,'0');
        var desc;
        if (value < 500) desc = 'Very similar (minimal change)';
        else if (value < 1500) desc = 'Similar (low change)';
        else if (value < 3000) desc = 'Moderate change';
        else if (value < 5000) desc = 'Significant change';
        else desc = 'Very high change';
        return {class_name: null, color: color, description: desc};
    }
    var datasetKey = resolveDatasetKey(layerId);
    var ds = LABEL_MAPPING[datasetKey] || {};
    if (ds.simplified_groups) {
        var sg = ds.simplified_groups;
        var gidx = sg.group_mapping[String(value)];
        if (gidx !== undefined) {
            return {class_name: sg.group_names[gidx], color: sg.group_colors[gidx]};
        }
        return {class_name: 'Unknown ('+value+')', color: '#000000'};
    }
    if (ds.label_mapping) {
        var idx = ds.label_mapping[String(value)];
        if (idx !== undefined && idx < (ds.class_names||[]).length) {
            return {
                class_name: ds.class_names[idx],
                color: idx < (ds.palette||[]).length ? ds.palette[idx] : '#000000',
            };
        }
        return {class_name: 'Unknown ('+value+')', color: '#000000'};
    }
    return {class_name: String(value), color: '#000000'};
}

// =====================================================================
// Sidebar rendering (was __SIDEBAR_DATASETS__ injection in app.py)
// =====================================================================
function renderSidebarDatasets(){
    var container = document.getElementById('sidebar-datasets');
    var html = '';
    for (var i=0; i<DATASETS.length; i++) {
        var ds = DATASETS[i];
        html += '<div class="dataset-group">';
        html += '<div class="dataset-header" onclick="toggleGroup(this)">';
        html += '<span class="arrow">\u25B8</span> ' + escapeHtml(ds.display_name);
        html += '</div><div class="dataset-years">';
        for (var j=0; j<ds.years.length; j++) {
            var y = ds.years[j];
            html += '<div class="year-item" id="yi-' + y.layer_id + '"';
            html += ' onclick="selectLayer(\'' + ds.key + '\', ' + y.year + ')">';
            html += y.year + '</div>';
        }
        html += '</div></div>';
    }
    container.innerHTML = html;
}

function escapeHtml(s){
    return String(s).replace(/[&<>"']/g, function(c){
        return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
}

// =====================================================================
// Legend rendering
// =====================================================================
function updateLegend(datasetKey){
    var div = legendControl._div;
    if (!datasetKey || !LEGEND_DATA[datasetKey]) { div.style.display='none'; return; }
    var d = LEGEND_DATA[datasetKey];
    var html = '<h4>' + escapeHtml(d.title) + '</h4>';
    if (d.type === 'continuous') {
        html += '<div class="legend-gradient" style="background:linear-gradient(to right,' + d.colors.join(',') + ')"></div>';
        html += '<div class="legend-gradient-labels"><span>' + escapeHtml(d.min_label) + '</span><span>' + escapeHtml(d.max_label) + '</span></div>';
    } else {
        for (var i=0; i<d.entries.length; i++) {
            var e = d.entries[i];
            html += '<div class="legend-entry"><div class="legend-swatch" style="background:' + e.color + '"></div><span>' + escapeHtml(e.name) + '</span></div>';
        }
    }
    div.innerHTML = html;
    div.style.display = 'block';
}

// =====================================================================
// Tile URL construction (was /api/activate)
// =====================================================================
function buildTileUrl(layerId){
    var entry = LAYER_INDEX[layerId];
    if (!entry) return null;
    var urlEnc = encodeURIComponent(entry.url);
    var base = TITILER_BASE + '/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=' + urlEnc;
    if (layerId === 'similarity') {
        // Continuous UInt16, green → yellow → red
        return base + '&rescale=0,7200&colormap_name=rdylgn_r';
    }
    // Categorical UInt8 with a custom sparse colormap
    var datasetKey = resolveDatasetKey(layerId);
    var cmap = buildColormap(datasetKey);
    return base + '&rescale=0,255&colormap=' + encodeURIComponent(JSON.stringify(cmap));
}

// =====================================================================
// Layer management
// =====================================================================
function showLoading(){document.getElementById('loading').classList.add('show')}
function hideLoading(){document.getElementById('loading').classList.remove('show')}

function selectLayer(datasetKey, year){
    var layerId = year !== null ? datasetKey + '_' + year : datasetKey;
    if (activeOverlay && activeOverlay.layerId === layerId) { clearActiveLayer(); return; }
    var tileUrl = buildTileUrl(layerId);
    if (!tileUrl) { alert('Unknown layer: ' + layerId); return; }
    applyLayer(datasetKey, year, layerId, tileUrl);
}

function applyLayer(datasetKey, year, layerId, tileUrl){
    if (activeOverlay && activeOverlay.tileLayer) map.removeLayer(activeOverlay.tileLayer);
    showLoading();
    var layer = L.tileLayer(tileUrl, {
        opacity: currentOpacity,
        maxZoom: 18,
        crossOrigin: true,
    });
    layer.on('load', hideLoading);
    layer.on('tileerror', hideLoading);
    layer.addTo(map);
    activeOverlay = {datasetKey: datasetKey, year: year, layerId: layerId, tileLayer: layer};
    updateLegend(datasetKey);
    updateSidebarHighlight(layerId);
    document.getElementById('clear-btn').classList.add('show');
    updateStatsVisibility();
}

function clearActiveLayer(){
    if (activeOverlay && activeOverlay.tileLayer) map.removeLayer(activeOverlay.tileLayer);
    activeOverlay = null;
    updateLegend(null);
    updateSidebarHighlight(null);
    document.getElementById('clear-btn').classList.remove('show');
    updateStatsVisibility();
}

// =====================================================================
// Sidebar interaction
// =====================================================================
function toggleGroup(el){
    el.classList.toggle('expanded');
    el.nextElementSibling.classList.toggle('show');
}

function updateSidebarHighlight(activeLayerId){
    document.querySelectorAll('.dataset-header').forEach(function(h){h.classList.remove('active')});
    document.querySelectorAll('.year-item').forEach(function(y){y.classList.remove('active')});
    document.getElementById('sim-item').classList.remove('active');
    if (!activeLayerId) return;
    if (activeLayerId === 'similarity') {
        document.getElementById('sim-item').classList.add('active');
        return;
    }
    var el = document.getElementById('yi-' + activeLayerId);
    if (el) {
        el.classList.add('active');
        var group = el.closest('.dataset-group');
        if (group) {
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
    if (name === currentBasemap) return;
    map.removeLayer(basemaps[currentBasemap]);
    basemaps[name].addTo(map);
    if (activeOverlay && activeOverlay.tileLayer) activeOverlay.tileLayer.bringToFront();
    currentBasemap = name;
    document.querySelectorAll('.basemap-option').forEach(function(o){o.classList.remove('active')});
    document.getElementById('bm-' + name).classList.add('active');
}

function setOpacity(val){
    currentOpacity = val / 100;
    document.getElementById('opacity-val').textContent = val + '%';
    if (activeOverlay && activeOverlay.tileLayer) activeOverlay.tileLayer.setOpacity(currentOpacity);
}

// =====================================================================
// Click → point query (was /api/query) + ESRI basemap date
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
        if (!d.results || !d.results.length) return null;
        var r0 = d.results[0], a = r0.attributes || {};
        var date = a.SRC_DATE2 || a.SRC_DATE || a['DATE (YYYYMMDD)'] || null;
        if (!date) return null;
        return {date: String(date), source: r0.layerName || a.DESCRIPTION || 'ESRI World Imagery'};
    }).catch(function(){return null});
}

map.on('click', function(e){
    if (activeDrawMode) return;
    var lat = e.latlng.lat, lng = e.latlng.lng;

    var pixelP = Promise.resolve(null);
    if (activeOverlay) {
        var entry = LAYER_INDEX[activeOverlay.layerId];
        if (entry) {
            var url = TITILER_BASE + '/cog/point/' + lng + ',' + lat + '?url=' + encodeURIComponent(entry.url);
            pixelP = fetch(url).then(function(r){return r.json()}).catch(function(){return null});
        }
    }
    var esriP = currentBasemap === 'esri' ? queryEsriImageryMeta(lat, lng) : Promise.resolve(null);

    Promise.all([pixelP, esriP]).then(function(parts){
        var pixel = parts[0], esri = parts[1];
        var decoded = null, val = null;
        if (pixel && pixel.values) {
            val = Math.round(pixel.values[0]);
            decoded = decodePixelValue(val, activeOverlay.layerId);
        }
        if (!decoded && !esri) return;

        var c = '<div class="pixel-info">';
        if (decoded) {
            if (decoded.class_name) {
                c += '<div><span class="pi-swatch" style="background:' + decoded.color + '"></span>';
                c += '<span class="pi-class">' + escapeHtml(decoded.class_name) + '</span></div>';
            } else if (decoded.description) {
                c += '<div><span class="pi-swatch" style="background:' + decoded.color + '"></span>';
                c += '<span class="pi-class">' + escapeHtml(decoded.description) + '</span></div>';
            }
            c += '<div>Pixel value: ' + val + '</div>';
        }
        if (esri) {
            c += '<div style="margin-top:4px;padding-top:4px;border-top:1px solid #eee;font-size:12px;color:#555">';
            c += 'World Imagery Date: ' + escapeHtml(esri.date) + '</div>';
        }
        c += '<div style="color:#888;font-size:11px">' + lat.toFixed(6) + ', ' + lng.toFixed(6) + '</div></div>';
        L.popup().setLatLng(e.latlng).setContent(c).openOn(map);
    });
});

// =====================================================================
// ToolBox — shared
// =====================================================================
function toggleToolSection(el){
    el.classList.toggle('expanded');
    el.nextElementSibling.classList.toggle('show');
}

// =====================================================================
// ToolBox — GeoJSON Import/Export
// =====================================================================
function startGeoJSONDraw(type){
    clearGeoJSON();
    if (type === 'point') {
        activeDrawMode = 'geojson-point';
        new L.Draw.Marker(map, {}).enable();
        document.getElementById('geojson-point-btn').classList.add('active');
    } else if (type === 'polygon') {
        activeDrawMode = 'geojson-poly';
        new L.Draw.Polygon(map, {shapeOptions:{color:'#ff6b6b',weight:2,fillOpacity:.1}}).enable();
        document.getElementById('geojson-poly-btn').classList.add('active');
    } else {
        activeDrawMode = 'geojson-rect';
        new L.Draw.Rectangle(map, {shapeOptions:{color:'#ff6b6b',weight:2,fillOpacity:.1}}).enable();
        document.getElementById('geojson-rect-btn').classList.add('active');
    }
}

function handleGeoJSONDrawn(e){
    geojsonLayer = e.layer;
    drawnItems.addLayer(geojsonLayer);
    ['geojson-point-btn','geojson-rect-btn','geojson-poly-btn'].forEach(function(id){
        document.getElementById(id).classList.remove('active');
    });
    document.getElementById('geojson-clear-btn').style.display = 'inline-block';
    document.getElementById('geojson-copy-btn').style.display = 'inline-block';
    document.getElementById('geojson-save-btn').style.display = 'inline-block';

    var g;
    if (e.layerType === 'marker') {
        var ll = e.layer.getLatLng();
        g = {type:'Point', coordinates:[round6(ll.lng), round6(ll.lat)]};
    } else if (e.layerType === 'polygon') {
        var latlngs = e.layer.getLatLngs()[0];
        var coords = latlngs.map(function(ll){return [round6(ll.lng), round6(ll.lat)]});
        coords.push(coords[0]);
        g = {type:'Polygon', coordinates:[coords]};
    } else {
        var b = e.layer.getBounds();
        g = {type:'Polygon', coordinates:[[
            [round6(b.getWest()), round6(b.getSouth())],
            [round6(b.getEast()), round6(b.getSouth())],
            [round6(b.getEast()), round6(b.getNorth())],
            [round6(b.getWest()), round6(b.getNorth())],
            [round6(b.getWest()), round6(b.getSouth())],
        ]]};
    }
    document.getElementById('geojson-output').value = JSON.stringify(g, null, 2);
}

function round6(n){ return parseFloat(n.toFixed(6)); }

function clearGeoJSON(){
    if (geojsonLayer) { drawnItems.removeLayer(geojsonLayer); geojsonLayer = null; }
    document.getElementById('geojson-output').value = '';
    document.getElementById('geojson-clear-btn').style.display = 'none';
    document.getElementById('geojson-copy-btn').style.display = 'none';
    document.getElementById('geojson-save-btn').style.display = 'none';
    ['geojson-point-btn','geojson-rect-btn','geojson-poly-btn'].forEach(function(id){
        document.getElementById(id).classList.remove('active');
    });
}

function copyGeoJSON(){
    var ta = document.getElementById('geojson-output');
    navigator.clipboard.writeText(ta.value).then(function(){
        var btn = document.getElementById('geojson-copy-btn');
        btn.textContent = 'Copied!';
        setTimeout(function(){btn.textContent = 'Copy to Clipboard'}, 1500);
    });
}

function saveGeoJSON(){
    var text = document.getElementById('geojson-output').value;
    if (!text) return;
    var blob = new Blob([text], {type:'application/json'});
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'geometry.json';
    a.click();
    URL.revokeObjectURL(a.href);
}

function handleFileUpload(input){
    if (!input.files || !input.files[0]) return;
    var file = input.files[0];
    var name = file.name.toLowerCase();
    var reader = new FileReader();
    if (name.endsWith('.zip') || name.endsWith('.shp')) {
        reader.onload = function(e){
            shp(e.target.result).then(displayGeoJSON).catch(function(err){
                alert('Failed to parse shapefile: ' + err);
            });
        };
        reader.readAsArrayBuffer(file);
    } else {
        reader.onload = function(e){
            try { displayGeoJSON(JSON.parse(e.target.result)); }
            catch (err) { alert('Failed to parse GeoJSON: ' + err); }
        };
        reader.readAsText(file);
    }
    input.value = '';
}

function displayGeoJSON(geojson){
    clearGeoJSON();
    geojsonLayer = L.geoJSON(geojson, {
        style: function(){return {color:'#ff6b6b',weight:2,fillOpacity:.1}},
        pointToLayer: function(f,ll){return L.marker(ll)},
    }).addTo(map);
    drawnItems.addLayer(geojsonLayer);
    map.fitBounds(geojsonLayer.getBounds());
    document.getElementById('geojson-output').value = JSON.stringify(geojson, null, 2);
    document.getElementById('geojson-clear-btn').style.display = 'inline-block';
    document.getElementById('geojson-copy-btn').style.display = 'inline-block';
    document.getElementById('geojson-save-btn').style.display = 'inline-block';
}

// =====================================================================
// ToolBox — Area Statistics (was /api/stats)
// =====================================================================
function updateStatsVisibility(){
    var noLayer = document.getElementById('stats-no-layer');
    var controls = document.getElementById('stats-controls');
    if (activeOverlay) { noLayer.style.display='none'; controls.style.display='block'; }
    else { noLayer.style.display='block'; controls.style.display='none'; clearStatsRect(); }
    document.getElementById('stats-results').innerHTML = '';
}

function startStatsDraw(type){
    if (!activeOverlay) return;
    clearStatsRect();
    if (type === 'poly') {
        activeDrawMode = 'stats-poly';
        new L.Draw.Polygon(map, {shapeOptions:{color:'#00d2ff',weight:2,fillOpacity:.15}}).enable();
        document.getElementById('stats-poly-btn').classList.add('active');
    } else {
        activeDrawMode = 'stats';
        new L.Draw.Rectangle(map, {shapeOptions:{color:'#00d2ff',weight:2,fillOpacity:.15}}).enable();
        document.getElementById('stats-draw-btn').classList.add('active');
    }
}

function clearStatsRect(){
    if (statsRect) { drawnItems.removeLayer(statsRect); statsRect = null; }
    document.getElementById('stats-results').innerHTML = '';
    document.getElementById('stats-clear-btn').style.display = 'none';
    document.getElementById('stats-draw-btn').classList.remove('active');
    document.getElementById('stats-poly-btn').classList.remove('active');
    document.getElementById('stats-stepper').style.display = 'none';
    lastStatsQuery = null;
}

function handleStatsFileUpload(input){
    if (!input.files || !input.files[0] || !activeOverlay) return;
    var file = input.files[0];
    var name = file.name.toLowerCase();
    var reader = new FileReader();
    if (name.endsWith('.zip') || name.endsWith('.shp')) {
        reader.onload = function(e){
            shp(e.target.result).then(applyStatsGeometry).catch(function(err){
                alert('Failed to parse shapefile: ' + err);
            });
        };
        reader.readAsArrayBuffer(file);
    } else {
        reader.onload = function(e){
            try { applyStatsGeometry(JSON.parse(e.target.result)); }
            catch (err) { alert('Failed to parse GeoJSON: ' + err); }
        };
        reader.readAsText(file);
    }
    input.value = '';
}

function applyStatsGeometry(geojson){
    clearStatsRect();
    statsRect = L.geoJSON(geojson, {
        style: function(){return {color:'#00d2ff',weight:2,fillOpacity:.15}},
    }).addTo(map);
    drawnItems.addLayer(statsRect);
    map.fitBounds(statsRect.getBounds());
    document.getElementById('stats-clear-btn').style.display = 'inline-block';
    fetchAreaStatsGeometry(geojson);
}

// Rectangular bbox → build a Polygon Feature and delegate to the geometry path.
function fetchAreaStats(bounds, layerIdOverride){
    var g = {
        type: 'Polygon',
        coordinates: [[
            [bounds.getWest(), bounds.getSouth()],
            [bounds.getEast(), bounds.getSouth()],
            [bounds.getEast(), bounds.getNorth()],
            [bounds.getWest(), bounds.getNorth()],
            [bounds.getWest(), bounds.getSouth()],
        ]],
    };
    lastStatsQuery = {type:'bbox', bounds: bounds};
    _runStats(g, layerIdOverride || activeOverlay.layerId);
}

function fetchAreaStatsGeometry(geojson, layerIdOverride){
    lastStatsQuery = {type:'geometry', geojson: geojson};
    _runStats(geojson, layerIdOverride || activeOverlay.layerId);
}

function _runStats(geojson, layerId){
    var entry = LAYER_INDEX[layerId];
    if (!entry) return;
    var el = document.getElementById('stats-results');
    document.getElementById('stats-loading').style.display = 'block';
    el.innerHTML = '';

    // Wrap bare geometry in a Feature — TiTiler accepts Feature or FeatureCollection.
    var body;
    if (geojson.type === 'Feature' || geojson.type === 'FeatureCollection') body = geojson;
    else body = {type:'Feature', geometry: geojson, properties: {}};

    var isCategorical = (layerId !== 'similarity');
    var url = TITILER_BASE + '/cog/statistics?url=' + encodeURIComponent(entry.url);
    if (isCategorical) url += '&categorical=true';

    fetch(url, {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(body),
    }).then(function(r){return r.json()}).then(function(resp){
        document.getElementById('stats-loading').style.display = 'none';
        if (resp.detail) {
            el.innerHTML = '<div style="color:#e74c3c">' + escapeHtml(JSON.stringify(resp.detail)) + '</div>';
            return;
        }
        var featureStats = (resp.properties ? resp.properties : resp).statistics;
        if (!featureStats || !featureStats.b1) {
            // FeatureCollection path: TiTiler returns features[] with per-feature stats
            if (resp.features && resp.features[0]) {
                featureStats = resp.features[0].properties.statistics;
            }
        }
        if (!featureStats || !featureStats.b1) {
            el.innerHTML = '<div style="color:#e74c3c">Unexpected stats response</div>';
            return;
        }
        var b1 = featureStats.b1;
        var geomForArea = (body.type === 'Feature') ? body.geometry : body.features[0].geometry;
        var pixelAreaKm2 = estimatePixelAreaKm2(geomForArea, b1);
        var result = isCategorical
            ? statsCategorical(b1, layerId, pixelAreaKm2)
            : statsContinuous(b1, pixelAreaKm2);
        renderStatsResults(result);
        updateStatsStepper();
    }).catch(function(err){
        document.getElementById('stats-loading').style.display = 'none';
        el.innerHTML = '<div style="color:#e74c3c">Request failed</div>';
    });
}

// Pixel area in km²: total geometry area / valid_pixel count.
// Uses a spherical-excess polygon area (good enough for Kenya-scale extents)
// divided by the number of pixels TiTiler reports.
function estimatePixelAreaKm2(geometry, b1){
    var validPixels = b1.valid_pixels || b1.count || 0;
    if (!validPixels) return 0;
    var areaM2 = geometryAreaSqMeters(geometry);
    return (areaM2 / validPixels) / 1e6;
}

// Simple spherical-polygon area (outer ring of a Polygon / MultiPolygon).
function geometryAreaSqMeters(g){
    if (!g) return 0;
    if (g.type === 'Polygon') return ringAreaSqMeters(g.coordinates[0]);
    if (g.type === 'MultiPolygon') {
        return g.coordinates.reduce(function(s, p){return s + ringAreaSqMeters(p[0])}, 0);
    }
    // Bare coord array
    if (Array.isArray(g) && Array.isArray(g[0]) && g[0].length >= 2) return ringAreaSqMeters(g);
    return 0;
}

function ringAreaSqMeters(coords){
    var R = 6378137;
    var area = 0;
    for (var i=0; i<coords.length-1; i++) {
        var p1 = coords[i], p2 = coords[i+1];
        area += (p2[0] - p1[0]) * Math.PI / 180 *
                (2 + Math.sin(p1[1]*Math.PI/180) + Math.sin(p2[1]*Math.PI/180));
    }
    area = Math.abs(area * R * R / 2);
    return area;
}

// Categorical: histogram=[[counts],[values]] (when categorical=true)
function statsCategorical(b1, layerId, pixelAreaKm2){
    var datasetKey = resolveDatasetKey(layerId);
    var ds = LABEL_MAPPING[datasetKey] || {};
    var hist = b1.histogram || [[],[]];
    var counts = hist[0] || [];
    var values = hist[1] || [];
    var total = 0;
    for (var i=0; i<counts.length; i++) total += counts[i];

    var rawClasses = [];
    for (var i=0; i<values.length; i++) {
        var val = Math.round(values[i]);
        var count = counts[i];
        if (!count) continue;
        var decoded = decodePixelValue(val, layerId);
        if (decoded.class_name === 'No Data') { total -= count; continue; }
        rawClasses.push({value: val, name: decoded.class_name, color: decoded.color, count: count});
    }

    // Merge duplicate names (simplified_groups maps many pixel values to one group)
    var merged = {};
    for (var i=0; i<rawClasses.length; i++) {
        var c = rawClasses[i];
        if (merged[c.name]) merged[c.name].count += c.count;
        else merged[c.name] = {name:c.name, color:c.color, count:c.count};
    }
    var classes = Object.values(merged).sort(function(a,b){return b.count - a.count});
    for (var i=0; i<classes.length; i++) {
        classes[i].pct = total > 0 ? (classes[i].count / total * 100) : 0;
        classes[i].area_km2 = +(classes[i].count * pixelAreaKm2).toFixed(2);
    }
    return {
        type: 'categorical',
        total_pixels: total,
        total_area_km2: +(total * pixelAreaKm2).toFixed(2),
        classes: classes,
    };
}

// Continuous (similarity): bucket into the same 5 ranges app.py uses.
function statsContinuous(b1, pixelAreaKm2){
    var total = b1.valid_pixels || b1.count || 0;
    if (!total) {
        return {type:'continuous', total_pixels:0, total_area_km2:0, buckets:[], min_val:0, max_val:0, mean_val:0};
    }
    var hist = b1.histogram || [[],[]];
    var counts = hist[0] || [];
    var edges = hist[1] || [];

    var bucketDefs = [
        [0, 500,    'Very similar (minimal change)', '#00ff00'],
        [500, 1500, 'Similar (low change)',          '#7fff00'],
        [1500, 3000,'Moderate change',               '#ffff00'],
        [3000, 5000,'Significant change',            '#ff7f00'],
        [5000, 1e9, 'Very high change',              '#ff0000'],
    ];

    // Apportion histogram bin counts to our 5 buckets by linear overlap.
    var buckets = bucketDefs.map(function(b){return {label:b[2], color:b[3], count:0}});
    for (var i=0; i<counts.length; i++) {
        var e0 = edges[i], e1 = edges[i+1];
        if (!(e1 > e0)) continue;
        var cnt = counts[i];
        if (!cnt) continue;
        for (var j=0; j<bucketDefs.length; j++) {
            var lo = bucketDefs[j][0], hi = bucketDefs[j][1];
            var ov = Math.max(0, Math.min(e1, hi) - Math.max(e0, lo));
            if (ov > 0) buckets[j].count += cnt * (ov / (e1 - e0));
        }
    }
    var filtered = [];
    for (var i=0; i<buckets.length; i++) {
        if (buckets[i].count > 0) {
            buckets[i].count = Math.round(buckets[i].count);
            buckets[i].pct = buckets[i].count / total * 100;
            buckets[i].area_km2 = +(buckets[i].count * pixelAreaKm2).toFixed(2);
            filtered.push(buckets[i]);
        }
    }
    return {
        type: 'continuous',
        total_pixels: total,
        total_area_km2: +(total * pixelAreaKm2).toFixed(2),
        min_val: Math.round(b1.min),
        max_val: Math.round(b1.max),
        mean_val: b1.mean,
        buckets: filtered,
    };
}

function fmtArea(km2){
    if (km2 >= 1) return km2.toLocaleString(undefined, {maximumFractionDigits:2}) + ' km\u00B2';
    return (km2 * 100).toFixed(2) + ' ha';
}

function renderStatsResults(data){
    var el = document.getElementById('stats-results');
    var html = '<div style="margin-bottom:6px;color:#aaa;font-size:.85em">Total area: ' + fmtArea(data.total_area_km2) + '</div>';
    if (data.type === 'categorical') {
        for (var i=0; i<data.classes.length; i++) {
            var c = data.classes[i];
            html += '<div class="stats-row">'
                 + '<div class="stats-swatch" style="background:' + c.color + '"></div>'
                 + '<span style="flex:1">' + escapeHtml(c.name) + '</span>'
                 + '<span style="text-align:right;white-space:nowrap">' + fmtArea(c.area_km2) + ' (' + c.pct.toFixed(1) + '%)</span>'
                 + '</div>'
                 + '<div style="background:#2a2a4a;border-radius:2px;height:8px;margin:2px 0 4px">'
                 + '<div class="stats-bar" style="width:' + c.pct + '%"></div></div>';
        }
    } else {
        html += '<div style="color:#aaa;font-size:.8em;margin-bottom:4px">'
             + 'Range: ' + data.min_val + ' \u2013 ' + data.max_val
             + ' (mean: ' + (data.mean_val||0).toFixed(1) + ')</div>';
        for (var i=0; i<data.buckets.length; i++) {
            var b = data.buckets[i];
            html += '<div class="stats-row">'
                 + '<div class="stats-swatch" style="background:' + b.color + '"></div>'
                 + '<span style="flex:1">' + escapeHtml(b.label) + '</span>'
                 + '<span style="text-align:right;white-space:nowrap">' + fmtArea(b.area_km2) + ' (' + b.pct.toFixed(1) + '%)</span>'
                 + '</div>'
                 + '<div style="background:#2a2a4a;border-radius:2px;height:8px;margin:2px 0 4px">'
                 + '<div class="stats-bar" style="width:' + b.pct + '%"></div></div>';
        }
    }
    el.innerHTML = html;
}

// =====================================================================
// Time-series stepper
// =====================================================================
function updateStatsStepper(){
    var stepper = document.getElementById('stats-stepper');
    if (!activeOverlay || activeOverlay.datasetKey === 'similarity' || !lastStatsQuery) {
        stepper.style.display = 'none'; return;
    }
    var years = DATASET_YEARS[activeOverlay.datasetKey];
    if (!years || years.length < 2) { stepper.style.display = 'none'; return; }
    stepper.style.display = 'block';
    document.getElementById('stats-step-year').textContent = activeOverlay.year;
    var idx = years.indexOf(activeOverlay.year);
    document.getElementById('stats-step-back').disabled = (idx >= years.length - 1);
    document.getElementById('stats-step-fwd').disabled  = (idx <= 0);
    document.getElementById('stats-step-range').textContent = years[years.length-1] + ' \u2013 ' + years[0];
}

function statsStepYear(direction){
    if (!activeOverlay || activeOverlay.datasetKey === 'similarity' || !lastStatsQuery) return;
    var years = DATASET_YEARS[activeOverlay.datasetKey];
    if (!years) return;
    var idx = years.indexOf(activeOverlay.year);
    var newIdx = idx - direction;
    if (newIdx < 0 || newIdx >= years.length) return;
    var newYear = years[newIdx];
    var newLayerId = activeOverlay.datasetKey + '_' + newYear;
    selectLayer(activeOverlay.datasetKey, newYear);
    if (lastStatsQuery.type === 'bbox') fetchAreaStats(lastStatsQuery.bounds, newLayerId);
    else fetchAreaStatsGeometry(lastStatsQuery.geojson, newLayerId);
}

// =====================================================================
// draw:created router
// =====================================================================
map.on('draw:created', function(e){
    if (activeDrawMode === 'stats') {
        statsRect = e.layer;
        drawnItems.addLayer(statsRect);
        document.getElementById('stats-draw-btn').classList.remove('active');
        document.getElementById('stats-clear-btn').style.display = 'inline-block';
        fetchAreaStats(statsRect.getBounds());
        activeDrawMode = null;
    } else if (activeDrawMode === 'stats-poly') {
        statsRect = e.layer;
        drawnItems.addLayer(statsRect);
        document.getElementById('stats-poly-btn').classList.remove('active');
        document.getElementById('stats-clear-btn').style.display = 'inline-block';
        var latlngs = e.layer.getLatLngs()[0];
        var coords = latlngs.map(function(ll){return [round6(ll.lng), round6(ll.lat)]});
        coords.push(coords[0]);
        fetchAreaStatsGeometry({type:'Polygon', coordinates:[coords]});
        activeDrawMode = null;
    } else if (activeDrawMode === 'geojson-point' || activeDrawMode === 'geojson-rect' || activeDrawMode === 'geojson-poly') {
        handleGeoJSONDrawn(e);
        activeDrawMode = null;
    }
});

setTimeout(function(){map.invalidateSize()}, 200);

// Kick off
bootstrap().catch(function(err){
    console.error('Bootstrap failed:', err);
    var sb = document.getElementById('sidebar-datasets');
    if (sb) {
        sb.innerHTML = '<div class="sidebar-error">'
            + 'Failed to load datasets.<br>'
            + '<span style="font-size:.8em;color:#888">' + escapeHtml(String(err)) + '</span><br>'
            + '<button onclick="location.reload()">Retry</button></div>';
    }
});

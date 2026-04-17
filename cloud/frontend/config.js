// Frontend runtime config. Swap TITILER_BASE at deploy time (e.g. to the Cloud Run URL).
window.KENYAMAP_CONFIG = {
    TITILER_BASE: 'http://localhost:8088',
    KENYA_CENTER: [0.0236, 37.9062],
    KENYA_ZOOM: 8,
};

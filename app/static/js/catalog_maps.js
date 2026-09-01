/**
 * catalog_maps.js
 *
 * Renders a Leaflet route map inside each .hike-card-map element on
 * the catalog page. The catalog template emits one card per hike,
 * each with a data-geojson attribute carrying that hike's cached
 * GeoJSON LineString. This script walks every such element on the
 * page and initializes a small map inside it.
 *
 * Maps here are thumbnails — all user interactions are disabled
 * (no scroll-wheel zoom, no drag, no double-click zoom). On a
 * catalog of N hikes, the user scrolling the page should scroll the
 * page, not accidentally zoom one of seven maps. To see the
 * interactive version, click through to the hike's detail page.
 *
 * Per-map attribution is suppressed; the catalog template renders
 * a single USGS attribution paragraph at the bottom of the page,
 * which satisfies the USGS terms once.
 */
(function () {
    if (typeof L === 'undefined') return;

    var els = document.querySelectorAll('.hike-card-map[data-geojson]');
    if (els.length === 0) return;

    els.forEach(function (el) {
        var geojson;
        try {
            geojson = JSON.parse(el.dataset.geojson);
        } catch (e) {
            console.error('Failed to parse hike GeoJSON for catalog map', e);
            return;
        }

        // All interactions disabled: scroll-wheel zoom and drag would
        // both fight with the user's page scroll across a grid of
        // maps; tap-to-zoom on mobile would do the same.
        var map = L.map(el, {
            maxZoom: 16,
            zoomControl: false,
            scrollWheelZoom: false,
            doubleClickZoom: false,
            dragging: false,
            touchZoom: false,
            keyboard: false,
            attributionControl: false
        });

        // Same USGS Topo basemap used everywhere else for visual
        // consistency. No attribution control here — page-level
        // attribution covers all maps.
        L.tileLayer(
            'https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}',
            { maxZoom: 16 }
        ).addTo(map);

        // Dark yellow + black halo polyline, matching the detail-page
        // route styling so the catalog feels like a preview of the
        // same map you'd see at full size when you click through.
        L.geoJSON(geojson, {
            style: { color: '#000', weight: 6, opacity: 0.55 }
        }).addTo(map);
        var fill = L.geoJSON(geojson, {
            style: { color: '#daa520', weight: 3, opacity: 1 }
        }).addTo(map);

        var bounds = fill.getBounds();
        if (bounds.isValid()) {
            // Slightly tight padding (10px each side) since each map
            // is a small thumbnail — generous padding wastes the
            // already-limited canvas.
            map.fitBounds(bounds, { padding: [10, 10] });
        }
    });
})();

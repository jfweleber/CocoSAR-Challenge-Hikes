/**
 * hike_map.js
 *
 * Renders the Leaflet route map on the hike detail page.
 *
 * Reads the server-cached GeoJSON LineString from the data-geojson
 * attribute on #hike-map (set by templates/hikes/detail.html). The
 * route was parsed once at hike-upload time and stored as GeoJSON
 * in the DB, so this script does no GPX/KML work — it just plots
 * the line.
 *
 * Companion file hike_elevation.js reads the same attribute to
 * render the elevation profile chart.
 */
(function () {
    var el = document.getElementById('hike-map');
    if (!el || typeof L === 'undefined') return;

    // Parse the route GeoJSON. If this ever fails we log and bail
    // rather than throwing — the elevation chart and the rest of the
    // page should still work even if the map can't be initialized.
    var geojson;
    try {
        geojson = JSON.parse(el.dataset.geojson);
    } catch (e) {
        console.error('Failed to parse hike GeoJSON for map', e);
        return;
    }

    // Static mode is opt-in via data-static="true" on the container
    // (set in templates/index.html for the home-page hero, omitted on
    // the hike detail page). When static, every user interaction is
    // disabled — the map becomes a decorative preview, and clicking
    // through to the detail page is how the user explores the route
    // interactively. Why this matters specifically: on mobile, an
    // interactive map captures touch-drag events meant to scroll the
    // page, which is maddening; the home hero is the worst offender
    // because the map sits above the rest of the content. Attribution
    // stays in both modes (USGS terms benefit from in-map attribution).
    var isStatic = el.dataset.static === 'true';
    var mapOpts = { maxZoom: 16 };
    if (isStatic) {
        mapOpts.zoomControl = false;
        mapOpts.scrollWheelZoom = false;
        mapOpts.doubleClickZoom = false;
        mapOpts.dragging = false;
        mapOpts.touchZoom = false;
        mapOpts.keyboard = false;
    }
    // maxZoom 16 matches the USGSTopo tile service's deepest level.
    // Setting it on both the map constructor AND the tile layer means
    // users physically can't zoom past where tiles exist.
    var map = L.map(el, mapOpts);

    // USGS National Map — Topo. Shows trails, contours, peaks, water
    // features, and government boundaries; more SAR-relevant than the
    // generic OSM raster basemap. ArcGIS REST tile service, so URL
    // template is {z}/{y}/{x} (y before x — Esri convention).
    // Attribution required by USGS terms.
    L.tileLayer(
        'https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}',
        {
            attribution: 'Tiles &copy; <a href="https://apps.nationalmap.gov/viewer/" target="_blank" rel="noopener">USGS The National Map</a>',
            maxZoom: 16
        }
    ).addTo(map);

    // Dark yellow (goldenrod) route with a black border halo. Two-
    // layer trick — wide-black underneath, thinner-yellow on top —
    // gives a high-contrast outlined line against USGS Topo's busy
    // green/brown/contour palette. Same styling pattern as user
    // tracks (in completion_edit_map.js) but a distinct hue, so on
    // the completion edit page the two stay visually separable.
    var routeBorder = L.geoJSON(geojson, {
        style: { color: '#000', weight: 6, opacity: 0.55 }
    }).addTo(map);
    var routeFill = L.geoJSON(geojson, {
        style: { color: '#daa520', weight: 3, opacity: 1 }
    }).addTo(map);

    var bounds = routeFill.getBounds();
    if (bounds.isValid()) {
        map.fitBounds(bounds, { padding: [20, 20] });
    } else {
        // Flagstaff fallback — CoCoSAR's home town and a sensible
        // default when bounds can't be derived (e.g. all coords
        // collapsed to a single point from a corrupt track).
        map.setView([35.198, -111.651], 12);
    }
})();

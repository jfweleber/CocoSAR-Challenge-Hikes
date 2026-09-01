/**
 * completion_edit_map.js
 *
 * Map view on the completion edit page. Renders the hike's official
 * route (dark yellow polyline with black border halo) and all of the
 * owner's uploaded tracks overlaid (orange polylines with the same
 * black border halo). Both layers get the high-contrast outlined
 * styling so they pop against USGS Topo's busy green/brown palette;
 * the distinct hues — yellow for route, orange for tracks — are what
 * keeps them visually separable where they overlap. The public per-
 * completion page uses the same overlay (see completion_map.js).
 *
 * Data is read from two data attributes on #edit-map:
 *   data-route   : the hike's route GeoJSON Feature (LineString)
 *   data-tracks  : a JSON array of GeoJSON Features, one per track
 */
(function () {
    var mapEl = document.getElementById('edit-map');
    if (!mapEl || typeof L === 'undefined') return;

    var route, tracks;
    try {
        route = JSON.parse(mapEl.dataset.route);
        tracks = JSON.parse(mapEl.dataset.tracks);
    } catch (e) {
        console.error('Failed to parse edit-map data attributes', e);
        return;
    }

    // maxZoom 16 matches the USGSTopo tile service's deepest level.
    var map = L.map(mapEl, { maxZoom: 16 });

    // USGS National Map — Topo. ArcGIS REST tile service, so URL
    // template is {z}/{y}/{x} (Esri convention, y before x).
    L.tileLayer(
        'https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}',
        {
            attribution: 'Tiles &copy; <a href="https://apps.nationalmap.gov/viewer/" target="_blank" rel="noopener">USGS The National Map</a>',
            maxZoom: 16
        }
    ).addTo(map);

    // Official hike route — dark yellow with black border halo. Two-
    // layer trick (wide-black underneath, thinner-yellow on top) for
    // high contrast against USGS Topo. Drawn first so the user's
    // tracks (next, in orange) sit on top.
    var routeBorder = L.geoJSON(route, {
        style: { color: '#000', weight: 6, opacity: 0.55 }
    }).addTo(map);
    var routeFill = L.geoJSON(route, {
        style: { color: '#daa520', weight: 3, opacity: 1 }
    }).addTo(map);

    // User's tracks: orange with a thin black border. Two overlapping
    // polylines — wide-black underneath, thinner-orange on top — gives
    // the appearance of an outlined line. SVG paths don't support
    // separate outer strokes, so this two-layer trick is the standard
    // workaround. FeatureCollection wrapper is the portable way to
    // hand multiple Features to L.geoJSON in one call.
    var trackFC = { type: 'FeatureCollection', features: tracks };
    var trackBorder = L.geoJSON(trackFC, {
        style: { color: '#000', weight: 6, opacity: 0.55 }
    }).addTo(map);
    var trackFill = L.geoJSON(trackFC, {
        style: { color: '#e07a1a', weight: 3, opacity: 1 }
    }).addTo(map);

    // Fit to the union of route + tracks. Extending an invalid bounds
    // is a no-op in Leaflet, so the check is safe when either layer
    // happens to have no usable geometry.
    var bounds = routeFill.getBounds();
    var trackBounds = trackFill.getBounds();
    if (trackBounds.isValid()) {
        bounds.extend(trackBounds);
    }
    if (bounds.isValid()) {
        map.fitBounds(bounds, { padding: [20, 20] });
    }
})();

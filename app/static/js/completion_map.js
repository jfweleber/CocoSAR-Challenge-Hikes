/**
 * completion_map.js
 *
 * Map on the PUBLIC per-completion page (/completions/<id>). Renders the
 * hike's official route (dark yellow polyline with a black border halo) and
 * the submitter's uploaded track(s) overlaid (orange, same halo). The two
 * distinct hues — yellow for the planned route, orange for what they actually
 * walked — stay separable where they overlap, and the black halo keeps both
 * legible over USGS Topo's busy green/brown palette.
 *
 * This mirrors completion_edit_map.js but reads from #completion-map and is
 * shown to everyone, not just the owner. (Individual tracks are public on the
 * completion page by design — see completions.view().)
 *
 * Data is read from two data attributes on #completion-map:
 *   data-route  : the hike's route GeoJSON Feature (LineString)
 *   data-tracks : a JSON array of GeoJSON Features, one per uploaded track
 */
(function () {
    var mapEl = document.getElementById('completion-map');
    if (!mapEl || typeof L === 'undefined') return;

    var route, tracks;
    try {
        route = JSON.parse(mapEl.dataset.route);
        tracks = JSON.parse(mapEl.dataset.tracks);
    } catch (e) {
        console.error('Failed to parse completion-map data attributes', e);
        return;
    }

    // maxZoom 16 matches the USGSTopo tile service's deepest level.
    var map = L.map(mapEl, { maxZoom: 16 });

    // USGS National Map — Topo. ArcGIS REST tile service, so {z}/{y}/{x}
    // (Esri convention, y before x). Attribution required by USGS terms.
    L.tileLayer(
        'https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}',
        {
            attribution: 'Tiles &copy; <a href="https://apps.nationalmap.gov/viewer/" target="_blank" rel="noopener">USGS The National Map</a>',
            maxZoom: 16
        }
    ).addTo(map);

    // Official hike route — black halo underneath, dark yellow on top. Drawn
    // first so the user's orange track sits visually above it.
    L.geoJSON(route, { style: { color: '#000', weight: 6, opacity: 0.55 } }).addTo(map);
    var routeFill = L.geoJSON(route, { style: { color: '#daa520', weight: 3, opacity: 1 } }).addTo(map);

    // Submitter's track(s): orange with the same black halo. A
    // FeatureCollection wrapper is the portable way to hand multiple Features
    // to a single L.geoJSON call.
    var trackFC = { type: 'FeatureCollection', features: tracks };
    L.geoJSON(trackFC, { style: { color: '#000', weight: 6, opacity: 0.55 } }).addTo(map);
    var trackFill = L.geoJSON(trackFC, { style: { color: '#e07a1a', weight: 3, opacity: 1 } }).addTo(map);

    // Fit to the union of route + tracks. Extending an invalid bounds is a
    // Leaflet no-op, so this is safe even if a layer has no usable geometry.
    var bounds = routeFill.getBounds();
    var trackBounds = trackFill.getBounds();
    if (trackBounds.isValid()) bounds.extend(trackBounds);
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [20, 20] });
})();

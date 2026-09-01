/**
 * profile_map.js
 *
 * Renders the consolidated track map on the user's /me profile page.
 *
 * Reads a JSON array of {hike_name, hike_slug, completed_on, geojson}
 * objects from the #profile-map element's data-tracks attribute. Each
 * track is drawn as two overlapping polylines — a black "border"
 * underneath (wider weight, semi-transparent) and an orange line on
 * top — so tracks remain legible against busy basemaps like USGS
 * Topo. Hovering a track shows a tooltip with the hike name and the
 * completion date; clicking takes you to that hike's detail page.
 *
 * Loaded by templates/profiles/me.html only when the user has at
 * least one track uploaded.
 */
(function () {
    var mapEl = document.getElementById('profile-map');
    if (!mapEl || typeof L === 'undefined') return;

    var tracks;
    try {
        tracks = JSON.parse(mapEl.dataset.tracks);
    } catch (e) {
        console.error('Failed to parse profile-map tracks', e);
        return;
    }
    if (!tracks || tracks.length === 0) return;

    // maxZoom 16 matches the USGSTopo tile service's deepest level.
    // Setting it on both the map constructor AND the tile layer means
    // users physically can't zoom past where tiles exist.
    var map = L.map(mapEl, { maxZoom: 16 });

    // USGS National Map — Topo. ArcGIS REST tile service, hence the
    // {z}/{y}/{x} order (y before x; Leaflet substitutes literal vars).
    // Attribution required by USGS terms.
    L.tileLayer(
        'https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}',
        {
            attribution: 'Tiles &copy; <a href="https://apps.nationalmap.gov/viewer/" target="_blank" rel="noopener">USGS The National Map</a>',
            maxZoom: 16
        }
    ).addTo(map);

    // Each track gets two overlapping layers — a thick semi-transparent
    // black "border" first, then the solid orange on top. SVG paths
    // don't support a separate outer stroke, so the two-layer trick is
    // the standard way to fake an outline on a polyline.
    var allLayers = [];
    tracks.forEach(function (t) {
        // Bottom: black border (wider so it pokes out as a ~1.5px halo
        // on each side of the orange line above).
        var border = L.geoJSON(t.geojson, {
            style: { color: '#000', weight: 6, opacity: 0.55 }
        }).addTo(map);

        // Top: orange fill. Tooltip + click handler live on the visible
        // color so the user interacts with what they're looking at.
        var tooltipText = t.hike_name +
            (t.completed_on ? ' — ' + t.completed_on : '');
        var fill = L.geoJSON(t.geojson, {
            style: { color: '#e07a1a', weight: 3, opacity: 1 }
        })
        .bindTooltip(tooltipText, { sticky: true })
        .on('click', function () {
            // Use a Flask-style URL. We don't know url_for here, but
            // the slug is in the data so we can reconstruct it.
            window.location.href = '/hikes/' + t.hike_slug;
        })
        .addTo(map);

        allLayers.push(border, fill);
    });

    // Fit the map to the bounding box of every track. featureGroup
    // gives us a getBounds() that's the union of its members.
    var group = L.featureGroup(allLayers);
    var bounds = group.getBounds();
    if (bounds.isValid()) {
        map.fitBounds(bounds, { padding: [20, 20] });
    }
})();

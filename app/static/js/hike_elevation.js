/**
 * hike_elevation.js
 *
 * Renders the elevation profile chart on the hike detail page.
 *
 * Walks the cached GeoJSON LineString stored on #hike-map's
 * data-geojson attribute, computing cumulative haversine distance
 * from the start for each point. Plots distance (miles) vs
 * elevation (feet) as a filled area chart with Chart.js.
 *
 * Companion to hike_map.js — both consume the same embedded
 * geometry so no extra HTTP request is needed for elevation data.
 */
(function () {
    var canvas = document.getElementById('hike-elevation');
    if (!canvas || typeof Chart === 'undefined') return;

    // The route GeoJSON is attached to the map container, not a
    // hidden div, because it's already there and the alternative
    // (a duplicate data attribute) just invites the two to drift.
    var src = document.getElementById('hike-map');
    if (!src) return;

    var geojson;
    try {
        geojson = JSON.parse(src.dataset.geojson);
    } catch (e) {
        console.error('Failed to parse hike GeoJSON for elevation chart', e);
        return;
    }

    var coords = (geojson.geometry && geojson.geometry.coordinates) || [];
    if (coords.length < 2) return;

    // ============================================================
    // STEP 1: Build the (distance, elevation) series
    // ============================================================
    // GPS tracks from watches/handhelds can run to ~10k points for
    // a half-day hike. That's fine for Chart.js with pointRadius=0
    // — it draws a polyline, not 10k markers. Distance accumulates
    // in meters internally, then we convert to miles for the chart
    // axis so units match the rest of the detail page.
    var series = [];
    var cumMeters = 0;
    var prev = coords[0];
    series.push({ x: 0, y: prev[2] * 3.28084 });
    for (var i = 1; i < coords.length; i++) {
        var cur = coords[i];
        cumMeters += haversine(prev[1], prev[0], cur[1], cur[0]);
        series.push({ x: cumMeters / 1609.344, y: cur[2] * 3.28084 });
        prev = cur;
    }

    // ============================================================
    // STEP 2: Render the chart
    // ============================================================
    new Chart(canvas, {
        type: 'line',
        data: {
            datasets: [{
                label: 'Elevation',
                data: series,
                borderColor: '#1f3a5f',
                backgroundColor: 'rgba(31, 58, 95, 0.15)',
                fill: true,
                pointRadius: 0,    // drawing the path, not the samples
                borderWidth: 2,
                tension: 0         // straight segments — the polyline is truth
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,  // parent .hike-elevation-wrap sets the height
            animation: false,
            interaction: { mode: 'nearest', intersect: false },
            scales: {
                x: {
                    type: 'linear',
                    title: { display: true, text: 'Distance (mi)' }
                },
                y: {
                    title: { display: true, text: 'Elevation (ft)' }
                }
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        // Default tooltip prints the raw {x, y}. Rewrite
                        // to a hiker-friendly "3.45 mi · 8210 ft" string.
                        label: function (ctx) {
                            return ctx.parsed.x.toFixed(2) + ' mi · ' +
                                   Math.round(ctx.parsed.y) + ' ft';
                        }
                    }
                }
            }
        }
    });


    /**
     * Great-circle distance in meters between two WGS84 lat/lon
     * points. Spherical-earth approximation (R = 6371 km); sub-meter
     * error at hike length, well below GPS noise. Matches the
     * haversine in app/track_parser.py so server-side and client-side
     * distance calculations agree.
     *
     * @returns {number} meters
     */
    function haversine(lat1, lon1, lat2, lon2) {
        var R = 6371000;
        var toRad = Math.PI / 180;
        var phi1 = lat1 * toRad;
        var phi2 = lat2 * toRad;
        var dphi = (lat2 - lat1) * toRad;
        var dlam = (lon2 - lon1) * toRad;
        var a = Math.sin(dphi / 2) * Math.sin(dphi / 2) +
                Math.cos(phi1) * Math.cos(phi2) *
                Math.sin(dlam / 2) * Math.sin(dlam / 2);
        return 2 * R * Math.asin(Math.sqrt(a));
    }
})();

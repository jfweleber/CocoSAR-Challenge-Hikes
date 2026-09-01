# ===============================================================================
# Module:   app/track_parser.py
# Purpose:  Parse GPX and KML uploads into a common GeoJSON Feature plus
#           computed distance and elevation gain. The Flask app stores
#           the parsed GeoJSON in the DB so per-page renders never need
#           to re-parse the source file.
#
#           Two cleanup steps happen here that the raw files don't do for
#           us: repeated positions are collapsed (see _dedupe), and
#           elevation gain is accumulated with a hysteresis threshold
#           (see _summarize). Both exist because real exports are noisy
#           in ways that quietly corrupt the headline numbers.
# Author:   Jamie F. Weleber
# Created:  May 18, 2026
# ===============================================================================
"""GPX and KML parsing to GeoJSON + summary stats."""

import io                              # wrap raw bytes as a file-like object for gpxpy
import math                            # trig for the haversine distance computation
from datetime import datetime, timezone  # UTC stamp in the exported GPX metadata
from xml.sax.saxutils import escape     # XML-escape hike names before they reach the document

import gpxpy                           # GPX parser; exposes .tracks/.segments/.points
from lxml import etree                 # XML parser with XPath, used for KML

# Earth's radius in meters used by the haversine formula. The mean radius
# is a sphere approximation; for trail-scale distances (a few miles to
# tens of miles) the error vs WGS84 ellipsoidal distance is under a
# meter — well below the noise floor of any consumer GPS receiver.
EARTH_RADIUS_M = 6371000.0

# Minimum rise, in meters, before a climb counts toward elevation gain.
#
# Without this, _summarize adds up every positive delta between adjacent
# points — and adjacent points can be under a meter apart. An Esri route
# export drapes elevation over a DEM, so each quantization step in that
# raster reads as a tiny climb; a GPS watch does the same thing with
# barometric jitter. Summed across thousands of samples it adds real
# altitude to a hike nobody climbed. Measured on the Flag Slabbath route
# (4.7 mi, 9,000 points): 2,671 ft unthresholded vs 2,369 ft of actual
# net climb, with only 52 ft of that excess coming from genuine dips.
#
# 3 m (~10 ft) sits in the range hiking platforms typically use. The exact
# value matters less than it looks: with the symmetric reference in
# _summarize, that route reports 2,516 ft at a 1 m threshold and 2,444 ft
# at 8 m — a 3% spread across an 8x change in the parameter. Anywhere in
# that band is defensible.
#
# Note this is applied at parse time, so it affects what gets STORED.
# Hikes and completion tracks parsed before this existed keep their
# original figures — there is deliberately no retroactive recompute.
ELEV_GAIN_THRESHOLD_M = 3.0

# OGC KML namespace. Most KML files declare this; some hand-written or
# legacy exports omit the namespace entirely, which is why _parse_kml
# has a no-namespace fallback below.
KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}


def parse_track(file_bytes, fmt):
    """Parse a GPX or KML file into (geojson_feature_dict, distance_m,
    elev_gain_m).

    The returned GeoJSON is a single Feature with a LineString geometry
    whose coordinates are [lon, lat, elev] triples (the order GeoJSON
    spec requires — lon first, then lat). Storing elevation in the
    third coordinate means the elevation profile chart can derive
    everything it needs from the geometry alone.

    Coordinates are de-duplicated and gain is thresholded on the way
    through — see _dedupe and _summarize for why both are necessary.

    Raises ValueError on an unsupported format string or a file with
    fewer than two usable track points (counted AFTER de-duplication).
    """
    fmt = fmt.lower()
    if fmt == "gpx":
        coords = _parse_gpx(file_bytes)
    elif fmt == "kml":
        coords = _parse_kml(file_bytes)
    else:
        raise ValueError(f"Unsupported format: {fmt}")

    # De-dupe BEFORE the length check, so a file that is nothing but one
    # position repeated 9,000 times fails with "no track points" rather
    # than being accepted as a zero-length track.
    coords = _dedupe(coords)
    if len(coords) < 2:
        raise ValueError("No track points found in file.")

    distance, gain = _summarize(coords)
    geojson = {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {},
    }
    return geojson, distance, gain


def build_gpx(coords, name, description=None, link=None):
    """[lon, lat, elev] coordinates -> a GPX 1.1 document, as a string.

    The rough inverse of parse_track, used to hand members a file they
    can load into Gaia, CalTopo, or a Garmin unit and follow on the
    ground.

    We generate from the stored coordinates rather than serving back the
    original upload, and that is a deliberate trade. It costs byte-for-
    byte fidelity with what the admin uploaded. It buys three things:
    output is always GPX no matter whether a GPX or a KML came in; the
    track carries the hike's own name instead of whatever the recorder
    happened to call it ("The Way Up", "Q3 2026 Challenge Hike"); and the
    repeated stationary positions _dedupe already stripped stay stripped
    -- on the Flag Slabbath route that is 9,000 points down to 1,800, a
    925 KB file down to roughly a fifth of that. Since none of the source
    files carry waypoints or routes, nothing else is lost in the round
    trip.

    Elevation is omitted entirely when every point reads exactly 0.0.
    That is the value _parse_gpx substitutes when a source file has no
    elevation data, and asserting sea level for a mountain trail is
    worse than saying nothing at all -- a consumer that sees no <ele>
    knows to fall back on its own terrain data.

    Coordinates are written to 6 decimal places (~0.11 m at this
    latitude), which is finer than any consumer GPS resolves and keeps
    the file small.
    """
    has_elev = any(round(c[2], 3) != 0.0 for c in coords)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="CocoSAR Challenge Hikes"',
        '     xmlns="http://www.topografix.com/GPX/1/1"',
        '     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '     xsi:schemaLocation="http://www.topografix.com/GPX/1/1',
        '                         http://www.topografix.com/GPX/1/1/gpx.xsd">',
        '  <metadata>',
        f'    <name>{escape(name)}</name>',
    ]
    if description:
        out.append(f'    <desc>{escape(description)}</desc>')
    if link:
        # The link element makes a file that gets passed around by text
        # message still say where it came from.
        out.append(f'    <link href="{escape(link)}"><text>{escape(name)}</text></link>')
    out += [
        f'    <time>{stamp}</time>',
        '  </metadata>',
        '  <trk>',
        f'    <name>{escape(name)}</name>',
        '    <trkseg>',
    ]
    for lon, lat, elev in coords:
        if has_elev:
            out.append(f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>{elev:.1f}</ele></trkpt>')
        else:
            out.append(f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}"/>')
    out += ['    </trkseg>', '  </trk>', '</gpx>', '']
    return "\n".join(out)


def _parse_gpx(data):
    """GPX -> list of [lon, lat, elev] coordinates.

    gpxpy gives us .tracks[].segments[].points with .latitude,
    .longitude, .elevation. We fall back to .routes[] if .tracks[]
    is empty — GPX files from "routing" apps (think Google Maps
    directions exported to GPX) put their geometry in routes rather
    than tracks.

    Elevation falls back to 0.0 when missing. Some recorders strip
    elevation when GPS lock is bad; we'd rather show a flat segment
    than reject the upload.
    """
    gpx = gpxpy.parse(io.BytesIO(data))
    coords = []
    for track in gpx.tracks:
        for seg in track.segments:
            for p in seg.points:
                coords.append([p.longitude, p.latitude, p.elevation or 0.0])
    if not coords:
        for route in gpx.routes:
            for p in route.points:
                coords.append([p.longitude, p.latitude, p.elevation or 0.0])
    return coords


def _parse_kml(data):
    """KML -> list of [lon, lat, elev] coordinates.

    KML's <coordinates> element holds the geometry as a single text
    string of whitespace-separated "lon,lat,alt" triples. We pull every
    <LineString>/<coordinates> we find — KML supports multiple
    LineStrings per Placemark and multiple Placemarks per file; we
    flatten them all into one list.

    The XPath query is run twice: first with the canonical KML
    namespace, then with no namespace at all (using local-name() to
    match the tag regardless of prefix). The second pass catches
    KML files that omit the xmlns declaration entirely — rare but
    not unheard of, especially from older or hand-written exporters.
    """
    root = etree.fromstring(data)
    coord_elems = root.xpath(".//kml:LineString/kml:coordinates", namespaces=KML_NS)
    if not coord_elems:
        coord_elems = root.xpath(
            ".//*[local-name()='LineString']/*[local-name()='coordinates']"
        )
    coords = []
    for elem in coord_elems:
        if not elem.text:
            continue
        # Tokens are whitespace-separated; each token is "lon,lat" or
        # "lon,lat,alt". Some KML files emit a trailing space which
        # split() handles cleanly. Altitude defaults to 0 if absent.
        for token in elem.text.split():
            parts = token.split(",")
            if len(parts) < 2:
                continue
            lon = float(parts[0])
            lat = float(parts[1])
            elev = float(parts[2]) if len(parts) > 2 else 0.0
            coords.append([lon, lat, elev])
    return coords


def _dedupe(coords):
    """Collapse runs of coordinates that repeat the same position.

    Why this is needed: exporters routinely emit the same lon/lat many
    times in a row. The Flag Slabbath route came out of Esri with 7,200
    of its 9,000 points being exact repeats of the point before — 80% of
    the file describing a hiker standing still. Three things go wrong
    with that:

      1. The stored GeoJSON is five times bigger than it needs to be
         (441 KB vs 88 KB for that route), and it gets inlined into a
         data attribute on the home page, the catalog card, AND the
         detail page. The catalog embeds EVERY hike's route in one
         document, so this compounds across the back catalog.

      2. Chart.js's tooltip uses `nearest` matching, which returns every
         element tied for closest. Coincident points tie exactly, so
         hovering the elevation profile popped up one identical row per
         repeat instead of a single reading.

      3. A stationary GPS with a drifting barometer reports elevation
         changes while the position never moves. Those are pure noise,
         and dropping the repeats drops the phantom gain with them.

    Matching is exact float equality on (lon, lat) rather than a distance
    tolerance, which keeps this strictly a de-duplication: it removes
    points that carry no information and never thins a real track. The
    first point of each run is the one kept.

    Distance and gain are unaffected for a well-formed track — a repeated
    position contributes zero horizontal distance by definition.
    """
    out = [coords[0]]
    for c in coords[1:]:
        if c[0] != out[-1][0] or c[1] != out[-1][1]:
            out.append(c)
    return out


def _summarize(coords):
    """Compute total horizontal distance (meters) and cumulative positive
    elevation gain (meters) over the polyline.

    Distance is a straight sum of point-to-point haversine hops.

    Gain is NOT a straight sum of positive deltas, and the difference
    matters. The naive version adds every upward step between adjacent
    points, which sounds right until you notice that adjacent points can
    be 80 cm apart and their elevations come from a DEM with its own
    quantization. Every one of those steps reads as a climb. Sum a few
    thousand of them and the route has gained hundreds of feet nobody
    walked up.

    Instead we track a running reference elevation and only bank a climb
    once it clears ELEV_GAIN_THRESHOLD_M above that reference — standard
    hysteresis.

    The reference moves in BOTH directions, and symmetry is the part worth
    understanding. An earlier cut dropped the reference on any descent at
    all, however small. That sounds harmless, but it means a sawtooth
    around a rising line resets the baseline into every trough and then
    banks the full trough-to-peak swing, so the jitter leaks straight back
    in. On a synthetic 400 m climb carrying 1.6 m of peak-to-peak noise it
    reported 460 m; moving the reference only when the excursion exceeds
    the threshold in either direction reports 401 m — the true figure.

    A genuine dip is unaffected either way: descending past the threshold
    walks the reference all the way down, so climbing back out counts in
    full. What gets filtered is only oscillation smaller than the
    threshold, which is noise by definition at hiking scale.

    Worked example from the Flag Slabbath route:
        naive sum          2,671 ft
        3 m threshold      2,461 ft
        actual net climb   2,369 ft   (trailhead to high point)
    The ~90 ft over net is real climbing: the route drops onto Pipeline
    and has to climb back out, which is ~72 ft of it.
    """
    distance = 0.0
    gain = 0.0
    prev = coords[0]
    # ref is the elevation the current climb is measured from. It ratchets
    # up each time we bank a rise, and drops the moment we descend below it.
    ref = coords[0][2]
    for cur in coords[1:]:
        distance += _haversine(prev[1], prev[0], cur[1], cur[0])
        rise = cur[2] - ref
        if rise > ELEV_GAIN_THRESHOLD_M:
            gain += rise
            ref = cur[2]
        elif ref - cur[2] > ELEV_GAIN_THRESHOLD_M:
            # Descending past the threshold: move the baseline down without
            # banking anything. This is what lets a real dip count in full
            # on the way back out.
            ref = cur[2]
        prev = cur
    return distance, gain


def _haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters between two WGS84 lat/lon points.

    Spherical-earth approximation with R = 6371 km. Sub-meter error
    at hike length, far below GPS noise. Matches the client-side
    haversine in static/js/hike_elevation.js so server and client
    distance computations agree exactly.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))

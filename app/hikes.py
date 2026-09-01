# ===============================================================================
# Module:   app/hikes.py
# Purpose:  Flask blueprint for Challenge Hike management. Public routes
#           expose the catalog and per-hike detail pages; admin routes
#           let an is_admin user add, edit, and delete hikes. The form
#           handler at the bottom is shared between admin_new and
#           admin_edit to avoid duplicating validation logic.
# Author:   Jamie F. Weleber
# Created:  May 18, 2026
# ===============================================================================
"""Public hike catalog + admin CRUD."""

import json                                # serializing parsed GeoJSON into the route_geojson TEXT column
import re                                  # slug normalization
import uuid                                # route filenames (UUIDs are path-safe and collision-free)
from functools import wraps                # preserve view function metadata in the admin_required decorator
from pathlib import Path                   # cross-platform path handling

from flask import (Blueprint, Response, abort, current_app, flash, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required

from .models import (create_hike, delete_hike, get_completion_for_user_hike,
                     get_featured_hike, get_hike_by_id, get_hike_by_slug,
                     get_hike_tallies, get_photos_for_completion,
                     list_completions_for_hike, list_completions_for_user,
                     list_hikes, update_hike)
from .track_parser import build_gpx, parse_track
from .timeutils import today_az            # Arizona-local "today" for the active/closed/upcoming state machine

bp = Blueprint("hikes", __name__)

# Extension -> internal format string. The schema's route_format column
# has CHECK (route_format IN ('gpx','kml')), so the stored values must
# match these literals exactly.
ROUTE_EXTS = {".gpx": "gpx", ".kml": "kml"}

# Slug normalizer: collapse any non-[a-z0-9] run into a single hyphen.
# Strips both leading and trailing hyphens after substitution.
SLUG_RE = re.compile(r"[^a-z0-9]+")


def admin_required(view):
    """View decorator: must be logged in AND is_admin = 1.

    Stacks the @login_required guard first (so an anonymous user gets
    the standard redirect-to-login flow) and then checks the admin
    bit. Non-admin logged-in users get a 403 instead of a redirect —
    they have a session, they're just not authorized.

    @wraps preserves the wrapped view's __name__ so Flask's URL routing
    and debug tooling show the original view name.
    """
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def slugify(text):
    """Lowercase + collapse non-alphanumeric runs to hyphens. Falls back
    to a literal 'hike' if the input has zero usable characters
    (avoids an empty-string slug that would fail the NOT NULL
    constraint). Admins can always override the auto-generated slug
    via the form's slug field."""
    s = SLUG_RE.sub("-", text.lower()).strip("-")
    return s or "hike"


# ===============================================================================
# STEP 1: Public routes
# ===============================================================================

@bp.route("/hikes")
def catalog():
    """All hikes, newest first, with a status pill computed in the template
    by lexicographic ISO date comparison against `today`.

    Three pill values now, and the distinction is worth being precise about
    since "past" no longer exists:

      upcoming — posted_on is still in the future; not yet doable
      newest   — the most recently posted hike, i.e. what the home page
                 features. A convenience flag, not a permission.
      open     — every other posted hike. Just as doable as the newest
                 one; the label only means "not the current headline."

    featured_id is fetched rather than derived from hikes[0] because the
    list is ordered by posted_on DESC across ALL hikes — so hikes[0] is a
    future-dated hike whenever one is queued, which is precisely the row
    that must NOT be labeled newest.

    When the viewer is logged in, also flag which hikes they've already
    completed so each card can show a "got the duck" badge. Anonymous
    viewers see no badges — there's no "you" to compare against — so
    we skip the completions query entirely in that path.
    """
    today_iso = today_az().isoformat()
    featured = get_featured_hike(today_iso)
    hikes = list_hikes()
    completed_hike_ids = set()
    if current_user.is_authenticated:
        completed_hike_ids = {c["hike_id"]
                              for c in list_completions_for_user(current_user.id)}
    return render_template("hikes/list.html",
                           hikes=hikes,
                           today=today_iso,
                           featured_id=featured["id"] if featured else None,
                           completed_hike_ids=completed_hike_ids)


@bp.route("/hikes/<slug>")
def detail(slug):
    """Single-hike detail page: route map, elevation chart, the state-
    aware completion CTA, and the public completion roll.

    The N+1 photo fetch (one query for completions, then one per
    completion for its photos) is fine at the small-team scale we're
    targeting — a hike with dozens of completions still resolves in
    a handful of millisecond-scale SQLite calls. Optimize later if
    needed; doing it now would be premature.
    """
    hike = get_hike_by_slug(slug)
    if not hike:
        abort(404)

    today_iso = today_az().isoformat()
    # Two states only — see hike_state() in completions.py for the same rule.
    # Duplicated as a one-liner rather than imported to avoid a circular
    # import between the two blueprints; it's a single comparison.
    state = "upcoming" if today_iso < hike["posted_on"] else "open"

    tallies = get_hike_tallies(hike["id"])

    completions = []
    for row in list_completions_for_hike(hike["id"]):
        completions.append({
            "row": row,
            "photos": get_photos_for_completion(row["id"]),
        })

    # Has the logged-in user already completed this hike? Drives the
    # CTA switch between "I completed this hike" and "View/edit your
    # completion" on the detail page.
    user_completion = None
    if current_user.is_authenticated:
        user_completion = get_completion_for_user_hike(current_user.id, hike["id"])

    return render_template("hikes/detail.html",
                           hike=hike, state=state, today=today_iso,
                           tallies=tallies,
                           completions=completions,
                           user_completion=user_completion)


@bp.route("/hikes/<slug>/route.gpx")
def route_gpx(slug):
    """Download the hike's route as a GPX file, for following on the ground.

    Built from the stored GeoJSON rather than the uploaded file — see
    build_gpx() for why that trade is worth making.

    Deliberately not gated on hike state. An `upcoming` hike already
    renders its full route on the detail page above, so withholding the
    file here would protect nothing; it would only make the button
    inconsistent with the map sitting next to it. If queued routes ever
    need to stay secret until they drop, the map is what has to change
    first, and this should follow it.

    No login required, matching the page itself. The uploaded route
    files are already world-readable at /uploads/tracks/<uuid>.gpx —
    this endpoint doesn't widen access, it just makes it findable and
    hands over a file named after the hike instead of a UUID.
    """
    hike = get_hike_by_slug(slug)
    if not hike:
        abort(404)

    coords = json.loads(hike["route_geojson"])["geometry"]["coordinates"]
    body = build_gpx(
        coords,
        name=hike["name"],
        description=(f"Coconino County Sheriff's SAR Mountain Rescue Unit "
                     f"Challenge Hike, posted {hike['posted_on']}."),
        link=url_for("hikes.detail", slug=hike["slug"], _external=True),
    )

    # The slug is already constrained to [a-z0-9-] by _slugify, so it
    # needs no further escaping to sit safely inside the header value.
    return Response(
        body,
        mimetype="application/gpx+xml",
        headers={
            "Content-Disposition": f'attachment; filename="{hike["slug"]}.gpx"',
        },
    )


# ===============================================================================
# STEP 2: Admin routes
# ===============================================================================

@bp.route("/admin/hikes")
@admin_required
def admin_list():
    """Admin dashboard listing every hike with Edit and Delete actions."""
    return render_template("hikes/admin_list.html", hikes=list_hikes())


@bp.route("/admin/hikes/new", methods=("GET", "POST"))
@admin_required
def admin_new():
    """Create a new hike. Route file (GPX or KML) is required."""
    if request.method == "POST":
        return _handle_form(None)
    return render_template("hikes/admin_form.html", hike=None)


@bp.route("/admin/hikes/<int:hike_id>/edit", methods=("GET", "POST"))
@admin_required
def admin_edit(hike_id):
    """Edit an existing hike. Route file is optional — uploading a
    new one replaces the existing route + its parsed GeoJSON cache;
    leaving the field blank keeps what's there."""
    hike = get_hike_by_id(hike_id)
    if not hike:
        abort(404)
    if request.method == "POST":
        return _handle_form(hike)
    return render_template("hikes/admin_form.html", hike=hike)


@bp.route("/admin/hikes/<int:hike_id>/delete", methods=("POST",))
@admin_required
def admin_delete(hike_id):
    """Delete a hike. FK CASCADE clears completion rows in the DB;
    the route file gets unlinked from disk explicitly. POST-only so
    a stray <img src="..."> or a browser prefetcher can't trigger it."""
    hike = get_hike_by_id(hike_id)
    if not hike:
        abort(404)
    # Remove the route file from disk before deleting the DB row.
    # If the DB delete fails, the file is gone but the row remains —
    # cosmetic glitch but no data loss. Reverse order would leave an
    # orphan file pointer in a DB row, which is worse.
    route_file = Path(current_app.config["UPLOAD_DIR"]) / "tracks" / hike["route_filename"]
    if route_file.exists():
        route_file.unlink()
    delete_hike(hike_id)
    flash(f'Hike "{hike["name"]}" deleted.', "success")
    return redirect(url_for("hikes.admin_list"))


# ===============================================================================
# STEP 3: Shared form handler
# ===============================================================================

def _handle_form(hike):
    """POST handler shared between admin_new (hike=None) and admin_edit
    (hike=<row>). The hike argument signals which flow we're in;
    branching happens at the DB-write step at the bottom.

    Validation is two-phase: validate everything up front (collect all
    errors so the user sees them at once), then commit files-to-disk
    and DB rows only after we know we'll succeed. Avoids a "half-
    created hike" where the DB has the row but the route file isn't
    on disk, or vice versa.

    A single posted_on date is all a hike carries. There's no end date to
    validate against, so the old "active_to must be >= active_from" check
    is gone with nothing replacing it — a posted_on in the future is
    perfectly valid and simply means the Challenge is queued for reveal.
    """
    name = (request.form.get("name") or "").strip()
    slug_input = (request.form.get("slug") or "").strip().lower()
    notes = (request.form.get("notes") or "").strip()
    posted_on = (request.form.get("posted_on") or "").strip()
    route_file = request.files.get("route")

    # ---- Sub-step A: Validate ----
    errors = []
    if not name:
        errors.append("Name is required.")
    if not posted_on:
        errors.append("Posted date is required.")

    final_slug = slug_input or slugify(name)
    # Slug character whitelist: lowercase ASCII letters, digits, hyphens
    # only. Anything else is rejected so URLs stay predictable and the
    # uniqueness check doesn't have to worry about case ambiguity.
    if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in final_slug):
        errors.append("Slug must use only lowercase letters, digits, and hyphens.")

    existing = get_hike_by_slug(final_slug)
    if existing and (hike is None or existing["id"] != hike["id"]):
        errors.append(f"Slug '{final_slug}' is already in use.")

    # Parse the route file up front so format/parse errors surface as
    # validation errors instead of blowing up half-way through the
    # commit step below.
    route_data = None
    if route_file and route_file.filename:
        ext = Path(route_file.filename).suffix.lower()
        fmt = ROUTE_EXTS.get(ext)
        if not fmt:
            errors.append("Route file must be .gpx or .kml.")
        else:
            try:
                file_bytes = route_file.read()
                geojson, distance, gain = parse_track(file_bytes, fmt)
                route_data = {
                    "filename": f"{uuid.uuid4().hex}{ext}",
                    "format": fmt,
                    "bytes": file_bytes,
                    "geojson": json.dumps(geojson),
                    "distance_m": distance,
                    "elev_gain_m": gain,
                }
            except Exception as exc:
                errors.append(f"Could not parse route file: {exc}")
    elif hike is None:
        errors.append("Route file is required for a new hike.")

    if errors:
        for msg in errors:
            flash(msg, "error")
        return render_template(
            "hikes/admin_form.html",
            hike=hike,
            form_values={"name": name, "slug": slug_input, "notes": notes,
                         "posted_on": posted_on},
        )

    # ---- Sub-step B: Commit ----
    if route_data:
        tracks_dir = Path(current_app.config["UPLOAD_DIR"]) / "tracks"
        (tracks_dir / route_data["filename"]).write_bytes(route_data["bytes"])

    if hike is None:
        # Create flow: insert a new hike row.
        create_hike(
            name=name, slug=final_slug, notes=notes,
            posted_on=posted_on,
            route_filename=route_data["filename"],
            route_format=route_data["format"],
            route_geojson=route_data["geojson"],
            distance_m=route_data["distance_m"],
            elev_gain_m=route_data["elev_gain_m"],
        )
        flash(f'Hike "{name}" created.', "success")
    else:
        # Edit flow: update_hike() applies only the named fields, so
        # we conditionally include route_* keys only when a new route
        # file was actually uploaded.
        update_kwargs = dict(name=name, slug=final_slug, notes=notes,
                             posted_on=posted_on)
        old_route_filename = hike["route_filename"]
        if route_data:
            update_kwargs.update(
                route_filename=route_data["filename"],
                route_format=route_data["format"],
                route_geojson=route_data["geojson"],
                distance_m=route_data["distance_m"],
                elev_gain_m=route_data["elev_gain_m"],
            )
        update_hike(hike["id"], **update_kwargs)
        # Clean up the orphaned old route file AFTER the DB update
        # succeeds. Doing it before would leave a DB row pointing at
        # a deleted file if the update threw.
        if route_data and old_route_filename != route_data["filename"]:
            old_file = Path(current_app.config["UPLOAD_DIR"]) / "tracks" / old_route_filename
            if old_file.exists():
                old_file.unlink()
        flash(f'Hike "{name}" updated.', "success")

    return redirect(url_for("hikes.detail", slug=final_slug))

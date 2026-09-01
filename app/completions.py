# ===============================================================================
# Module:   completions.py
# Purpose:  Flask blueprint for the completion-submission flow. Logged-in users
#           upload proof (photos and/or GPX/KML tracks) of a Challenge Hike
#           they've done, self-attesting the date via a calendar field that's
#           bounded to (posted date .. today). One completion per user per
#           hike — enforced both by the UNIQUE(user_id, hike_id) DB constraint
#           and by an early redirect on the submit route.
#
#           Hikes never close. The only thing that can block a submission is
#           a hike that hasn't been posted yet; everything already posted
#           accepts completions indefinitely.
# Author:   Jamie F. Weleber
# Created:  May 18, 2026
# ===============================================================================
"""Submit, edit, and delete Challenge Hike completions."""

import json                                # serializing parsed GeoJSON into a TEXT DB column
import uuid                                # filenames for uploaded files (avoids collision + sanitization)
from pathlib import Path                   # cross-platform path handling for upload dirs

from flask import (Blueprint, abort, current_app, flash, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required

from .models import (
    User,
    add_completion_photo, add_completion_track,
    create_completion, delete_completion,
    delete_photo, delete_track,
    get_completion, get_completion_for_user_hike,
    get_hike_by_id, get_hike_by_slug,
    get_photo, get_photos_for_completion,
    get_track, get_tracks_for_completion,
    set_completion_counts,
    update_completion,
)
from .mail import notify_admin                       # operator notification on new submissions
from .photo_utils import (PHOTO_EXTS, PHOTO_MAX_EDGE,  # shared HEIC/EXIF/resize pipeline
                          process_photo, thumb_dir_for)
from .track_parser import parse_track
from .timeutils import today_az                      # Arizona-local "today" for the active-window check

bp = Blueprint("completions", __name__)

# Track file formats. Map extension -> internal format string stored in the
# completion_tracks.format column (matches the CHECK constraint in models.py).
TRACK_EXTS = {".gpx": "gpx", ".kml": "kml"}


# ===============================================================================
# STEP 1: Helpers
# ===============================================================================

def hike_state(hike, today_iso):
    """Return 'upcoming' or 'open' for a hike vs today.

    Only two states exist now. A hike is 'upcoming' until its posted_on
    arrives and 'open' forever after — there is no third state, because
    nothing ever closes. Anywhere the old 'closed' state used to branch,
    the correct new behavior is simply to do what 'active' did.

    Comparing ISO date strings lexicographically works because we store
    dates as YYYY-MM-DD. A different format (e.g. M/D/YYYY) would need
    real date parsing here.
    """
    return "upcoming" if today_iso < hike["posted_on"] else "open"


def validate_completed_on(value, hike, today_iso):
    """Return an error message if the self-attested date is unacceptable,
    or None if it passes.

    Two bounds, both open-ended on the far side:

      - Not before the hike was posted. You can't retroactively claim a
        Challenge with a hike you did before the Challenge existed — the
        duck is for doing the route *because* it was posted, and that's
        the one piece of the old window rule worth keeping.

      - Not in the future. Stops someone pre-claiming a hike they intend
        to do this weekend.

    Everything between those two is fair game, which is the point: a
    member who joins in 2027 can work the whole back catalog at their
    own pace instead of finding every past route locked.
    """
    if not value:
        return "Please enter the date you completed the hike."
    if value < hike["posted_on"]:
        return (f"Completion date can't be before this Challenge was "
                f"posted on {hike['posted_on']}.")
    if value > today_iso:
        return "Completion date can't be in the future."
    return None


# ===============================================================================
# STEP 2: Submission
# ===============================================================================

@bp.route("/hikes/<slug>/complete", methods=("GET", "POST"))
@login_required
def submit(slug):
    """Show the new-completion form (GET) and process it (POST).

    Routes around two edge cases up front:
      - Upcoming hike: flash an explanation, send back to the hike page.
      - User already has a completion: redirect to edit. Lets the user
        amend rather than collide with the UNIQUE(user_id, hike_id) constraint.
    """
    hike = get_hike_by_slug(slug)
    if not hike:
        abort(404)

    today_iso = today_az().isoformat()
    state = hike_state(hike, today_iso)

    if state == "upcoming":
        flash(f"This Challenge drops on {hike['posted_on']}.", "error")
        return redirect(url_for("hikes.detail", slug=slug))

    existing = get_completion_for_user_hike(current_user.id, hike["id"])
    if existing:
        flash("You've already submitted for this hike — you can edit your "
              "completion below.", "info")
        return redirect(url_for("completions.edit", completion_id=existing["id"]))

    if request.method != "POST":
        # The picker spans posted_on .. today. Both bounds are UX hints only;
        # validate_completed_on() is the authoritative gate on the POST.
        return render_template(
            "completions/submit.html",
            hike=hike, state=state, today=today_iso,
            max_date=today_iso,
        )

    return _save_new_completion(hike, state, today_iso)


def _save_new_completion(hike, state, today_iso):
    """POST handler for submit(). Validates everything up front, then commits
    files-to-disk and DB rows together once we know the whole submission
    will succeed."""
    completed_on = (request.form.get("completed_on") or "").strip()
    comment = (request.form.get("comment") or "").strip() or None

    # request.files.getlist tolerates the input being entirely absent (returns
    # []) and also strips out the "empty FileStorage" that browsers send for
    # unfilled file inputs.
    photos = [f for f in request.files.getlist("photos") if f and f.filename]
    tracks = [f for f in request.files.getlist("tracks") if f and f.filename]

    # ---- Sub-step A: Validate everything before touching disk ----
    errors = []
    date_error = validate_completed_on(completed_on, hike, today_iso)
    if date_error:
        errors.append(date_error)
    if not photos and not tracks:
        errors.append("Please upload at least one photo or one track as proof.")

    # Upload caps: at most MAX_PHOTOS_PER_COMPLETION photos, each within
    # MAX_PHOTO_BYTES. Tracks are tiny by comparison and aren't individually
    # capped; MAX_CONTENT_LENGTH backstops the request as a whole.
    max_photos = current_app.config["MAX_PHOTOS_PER_COMPLETION"]
    max_photo_bytes = current_app.config["MAX_PHOTO_BYTES"]
    if len(photos) > max_photos:
        errors.append(
            f"Too many photos ({len(photos)}). Up to {max_photos} per completion — "
            "add any extras afterward via Edit."
        )

    for photo_file in photos:
        ext = Path(photo_file.filename).suffix.lower()
        if ext not in PHOTO_EXTS:
            errors.append(f"Photo {photo_file.filename!r}: unsupported format {ext}.")
        # Per-photo size cap. Seek to the end to measure, then rewind so the
        # later process_photo() still reads the file from the start.
        photo_file.stream.seek(0, 2)   # 2 = SEEK_END
        size = photo_file.stream.tell()
        photo_file.stream.seek(0)
        if size > max_photo_bytes:
            errors.append(
                f"Photo {photo_file.filename!r} is {size // (1024 * 1024)} MB — "
                f"limit is {max_photo_bytes // (1024 * 1024)} MB per photo."
            )

    # Parse tracks now so we can flag parse errors as validation errors
    # rather than blowing up halfway through the commit step. We keep the
    # parsed result around so we don't parse twice.
    parsed_tracks = []
    for track_file in tracks:
        ext = Path(track_file.filename).suffix.lower()
        fmt = TRACK_EXTS.get(ext)
        if not fmt:
            errors.append(f"Track {track_file.filename!r} must be .gpx or .kml.")
            continue
        try:
            file_bytes = track_file.read()
            geojson, distance, gain = parse_track(file_bytes, fmt)
        except Exception as exc:
            errors.append(f"Could not parse {track_file.filename!r}: {exc}")
            continue
        parsed_tracks.append({
            "filename": f"{uuid.uuid4().hex}{ext}",
            "format": fmt,
            "bytes": file_bytes,
            "geojson": json.dumps(geojson),
            "distance_m": distance,
            "elev_gain_m": gain,
        })

    if errors:
        for msg in errors:
            flash(msg, "error")
        return render_template(
            "completions/submit.html",
            hike=hike, state=state, today=today_iso,
            max_date=today_iso,
            form_values={"completed_on": completed_on, "comment": comment},
        )

    # ---- Sub-step B: Commit files-to-disk and DB rows ----
    photos_dir = Path(current_app.config["UPLOAD_DIR"]) / "photos"
    thumbs_dir = thumb_dir_for(photos_dir)      # uploads/photos/thumbs, created if missing
    tracks_dir = Path(current_app.config["UPLOAD_DIR"]) / "tracks"

    completion_id = create_completion(
        user_id=current_user.id,
        hike_id=hike["id"],
        completed_on=completed_on,
        comment=comment,
    )

    for photo_file in photos:
        # Stream pointer may be at EOF after browser/werkzeug buffering.
        # Rewind defensively before Pillow reads it.
        photo_file.stream.seek(0)
        try:
            # Stored copy capped at PHOTO_MAX_EDGE plus a grid thumbnail —
            # see the SIZING block in photo_utils for why both exist.
            filename, thumb = process_photo(photo_file, photos_dir,
                                            max_edge=PHOTO_MAX_EDGE,
                                            thumb_dir=thumbs_dir)
        except Exception as exc:
            # A single bad photo doesn't roll back the rest of the submission.
            # The completion + good photos + tracks remain; user can re-add
            # the failed photo later via edit.
            flash(f"Saving {photo_file.filename!r} failed: {exc}", "error")
            continue
        add_completion_photo(completion_id, filename, thumb_filename=thumb)

    for t in parsed_tracks:
        (tracks_dir / t["filename"]).write_bytes(t["bytes"])
        add_completion_track(
            completion_id=completion_id,
            filename=t["filename"], fmt=t["format"],
            geojson=t["geojson"],
            distance_m=t["distance_m"], elev_gain_m=t["elev_gain_m"],
        )

    # Tell the operator. Deliberately create-only — notifying on every edit
    # would mean an email each time somebody fixes a typo or adds a photo,
    # which trains you to ignore the whole channel.
    #
    # Photo and track counts are read back from the DB rather than taken
    # from the loops above, because a photo that failed processing gets
    # skipped there; this reports what actually landed.
    notify_admin(
        f"{current_user.name} completed {hike['name']}",
        f"{current_user.name} logged a completion of {hike['name']}.\n"
        f"Hiked: {completed_on}\n"
        f"Photos: {len(get_photos_for_completion(completion_id))}   "
        f"Tracks: {len(get_tracks_for_completion(completion_id))}\n"
        + (f"Comment: {comment}\n" if comment else "")
        + f"\n{url_for('completions.view', completion_id=completion_id, _external=True)}\n"
    )

    flash("Completion recorded — you've earned the duck.", "success")
    return redirect(url_for("hikes.detail", slug=hike["slug"]))


# ===============================================================================
# STEP 3: Edit
# ===============================================================================

@bp.route("/completions/<int:completion_id>/edit", methods=("GET", "POST"))
@login_required
def edit(completion_id):
    """Edit an existing completion. Owner or admin only.

    The edit form differs from submit in that it shows existing photos
    and tracks with per-row "Remove" checkboxes, alongside file inputs
    for adding new ones. There's no window to re-check on the way in —
    hikes don't close — so the only date rule that still applies is the
    same posted_on..today bound the submit form uses.
    """
    completion = get_completion(completion_id)
    if not completion:
        abort(404)
    if completion["user_id"] != current_user.id and not current_user.is_admin:
        abort(403)

    hike = get_hike_by_id(completion["hike_id"])
    photos = get_photos_for_completion(completion_id)
    tracks = get_tracks_for_completion(completion_id)

    if request.method != "POST":
        today_iso = today_az().isoformat()
        return render_template(
            "completions/edit.html",
            completion=completion, hike=hike,
            photos=photos, tracks=tracks,
            max_date=today_iso,
            track_geojsons=[json.loads(t["track_geojson"]) for t in tracks],
        )

    return _save_edit(completion, hike, photos, tracks)


def _save_edit(completion, hike, existing_photos, existing_tracks):
    """POST handler for edit(). Mirrors the submission flow's two-phase
    validate-then-commit structure."""
    today_iso = today_az().isoformat()

    completed_on = (request.form.get("completed_on") or "").strip()
    comment = (request.form.get("comment") or "").strip() or None

    # Form checkboxes for removal yield string IDs; coerce to int set for
    # set-arithmetic against the existing rows. Non-digit values get filtered
    # so a tampered form can't crash the int() call.
    remove_photo_ids = {int(i) for i in request.form.getlist("remove_photos") if i.isdigit()}
    remove_track_ids = {int(i) for i in request.form.getlist("remove_tracks") if i.isdigit()}

    new_photos = [f for f in request.files.getlist("photos") if f and f.filename]
    new_tracks = [f for f in request.files.getlist("tracks") if f and f.filename]

    errors = []
    date_error = validate_completed_on(completed_on, hike, today_iso)
    if date_error:
        errors.append(date_error)

    # Resulting-state proof check: after removals and additions, the
    # completion must still have at least one photo or track.
    remaining_photos = len(existing_photos) - len(remove_photo_ids) + len(new_photos)
    remaining_tracks = len(existing_tracks) - len(remove_track_ids) + len(new_tracks)
    if remaining_photos == 0 and remaining_tracks == 0:
        errors.append("A completion must keep at least one photo or one track.")

    # Same caps as submit, applied to the RESULTING photo set after removals + adds.
    max_photos = current_app.config["MAX_PHOTOS_PER_COMPLETION"]
    max_photo_bytes = current_app.config["MAX_PHOTO_BYTES"]
    if remaining_photos > max_photos:
        errors.append(
            f"That would leave {remaining_photos} photos. A completion can have at "
            f"most {max_photos}; remove some before adding more."
        )

    for photo_file in new_photos:
        ext = Path(photo_file.filename).suffix.lower()
        if ext not in PHOTO_EXTS:
            errors.append(f"Photo {photo_file.filename!r}: unsupported format {ext}.")
        # Per-photo size cap (measure-then-rewind so process_photo() still works).
        photo_file.stream.seek(0, 2)   # 2 = SEEK_END
        size = photo_file.stream.tell()
        photo_file.stream.seek(0)
        if size > max_photo_bytes:
            errors.append(
                f"Photo {photo_file.filename!r} is {size // (1024 * 1024)} MB — "
                f"limit is {max_photo_bytes // (1024 * 1024)} MB per photo."
            )

    parsed_tracks = []
    for track_file in new_tracks:
        ext = Path(track_file.filename).suffix.lower()
        fmt = TRACK_EXTS.get(ext)
        if not fmt:
            errors.append(f"Track {track_file.filename!r} must be .gpx or .kml.")
            continue
        try:
            file_bytes = track_file.read()
            geojson, distance, gain = parse_track(file_bytes, fmt)
        except Exception as exc:
            errors.append(f"Could not parse {track_file.filename!r}: {exc}")
            continue
        parsed_tracks.append({
            "filename": f"{uuid.uuid4().hex}{ext}",
            "format": fmt,
            "bytes": file_bytes,
            "geojson": json.dumps(geojson),
            "distance_m": distance,
            "elev_gain_m": gain,
        })

    if errors:
        for msg in errors:
            flash(msg, "error")
        return redirect(url_for("completions.edit", completion_id=completion["id"]))

    photos_dir = Path(current_app.config["UPLOAD_DIR"]) / "photos"
    thumbs_dir = thumb_dir_for(photos_dir)
    tracks_dir = Path(current_app.config["UPLOAD_DIR"]) / "tracks"

    # ---- Apply removals ----
    # Verify each item belongs to this completion before deleting. The form
    # exposes the IDs as hidden values, so a tampered POST could otherwise
    # request deletion of someone else's photo. This check is the gate.
    for pid in remove_photo_ids:
        photo = get_photo(pid)
        if photo and photo["completion_id"] == completion["id"]:
            (photos_dir / photo["filename"]).unlink(missing_ok=True)
            # Thumb is NULL for anything uploaded before the thumbnail work
            # (or for an animated GIF, which we never thumbnail), so guard
            # before unlinking. missing_ok covers the rest.
            if photo["thumb_filename"]:
                (thumbs_dir / photo["thumb_filename"]).unlink(missing_ok=True)
            delete_photo(pid)
    for tid in remove_track_ids:
        track = get_track(tid)
        if track and track["completion_id"] == completion["id"]:
            (tracks_dir / track["filename"]).unlink(missing_ok=True)
            delete_track(tid)

    # ---- Apply additions ----
    for photo_file in new_photos:
        photo_file.stream.seek(0)
        try:
            filename, thumb = process_photo(photo_file, photos_dir,
                                            max_edge=PHOTO_MAX_EDGE,
                                            thumb_dir=thumbs_dir)
        except Exception as exc:
            flash(f"Saving {photo_file.filename!r} failed: {exc}", "error")
            continue
        add_completion_photo(completion["id"], filename, thumb_filename=thumb)
    for t in parsed_tracks:
        (tracks_dir / t["filename"]).write_bytes(t["bytes"])
        add_completion_track(
            completion_id=completion["id"],
            filename=t["filename"], fmt=t["format"],
            geojson=t["geojson"],
            distance_m=t["distance_m"], elev_gain_m=t["elev_gain_m"],
        )

    # ---- Apply metadata changes ----
    update_completion(completion["id"], completed_on=completed_on, comment=comment)

    # Admin-only: whether this completion counts toward ducks + leaderboard. The
    # checkbox only renders for admins, and we only honor it for admins here, so an
    # owner editing their own completion can never (un)flag their own duck.
    if current_user.is_admin:
        set_completion_counts(completion["id"], "counts" in request.form)

    flash("Completion updated.", "success")
    return redirect(url_for("hikes.detail", slug=hike["slug"]))


# ===============================================================================
# STEP 4: Delete
# ===============================================================================

@bp.route("/completions/<int:completion_id>/delete", methods=("POST",))
@login_required
def delete(completion_id):
    """Delete a completion. Owner or admin only.

    FK ON DELETE CASCADE on completion_photos and completion_tracks
    handles the DB rows automatically. The actual files on disk need
    to be unlinked manually — we fetch the row sets before the DELETE
    so we still have the filenames after the cascade.
    """
    completion = get_completion(completion_id)
    if not completion:
        abort(404)
    if completion["user_id"] != current_user.id and not current_user.is_admin:
        abort(403)

    hike = get_hike_by_id(completion["hike_id"])

    photos_dir = Path(current_app.config["UPLOAD_DIR"]) / "photos"
    thumbs_dir = thumb_dir_for(photos_dir)
    tracks_dir = Path(current_app.config["UPLOAD_DIR"]) / "tracks"
    for p in get_photos_for_completion(completion_id):
        (photos_dir / p["filename"]).unlink(missing_ok=True)
        if p["thumb_filename"]:
            (thumbs_dir / p["thumb_filename"]).unlink(missing_ok=True)
    for t in get_tracks_for_completion(completion_id):
        (tracks_dir / t["filename"]).unlink(missing_ok=True)
    delete_completion(completion_id)

    flash("Completion deleted.", "success")
    return redirect(url_for("hikes.detail", slug=hike["slug"]))


# ===============================================================================
# STEP 5: Public completion view
# ===============================================================================

@bp.route("/completions/<int:completion_id>")
def view(completion_id):
    """Public per-completion page: the submitter's track(s) drawn over the
    hike's official route, plus the full photo gallery in a lightbox.

    Anyone can view it — no login required. Edit/delete controls are shown
    only to the owner or an admin (can_edit). Individual tracks are public
    here by design; this is a deliberate change from the earlier 'tracks are
    private' posture (the photos were already public on the hike detail page).
    """
    completion = get_completion(completion_id)
    if not completion:
        abort(404)

    user = User.by_id(completion["user_id"])
    hike = get_hike_by_id(completion["hike_id"])
    photos = get_photos_for_completion(completion_id)
    tracks = get_tracks_for_completion(completion_id)
    can_edit = current_user.is_authenticated and (
        current_user.id == completion["user_id"] or current_user.is_admin
    )

    return render_template(
        "completions/view.html",
        completion=completion, user=user, hike=hike,
        photos=photos, tracks=tracks,
        # Decode each track's GeoJSON once here so the template can dump the
        # whole list with a single | tojson for completion_map.js.
        track_geojsons=[json.loads(t["track_geojson"]) for t in tracks],
        can_edit=can_edit,
    )

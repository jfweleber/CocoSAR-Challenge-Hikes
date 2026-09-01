# ===============================================================================
# Module:   profiles.py
# Purpose:  User profile views. /users/<id> is the public profile (anyone
#           can view a member's duck count, aggregated track map, and
#           completed-hike list); /me is the same page for the logged-in
#           user with owner-only edit controls shown; /me/edit lets a user
#           change their display name and avatar. All three share one
#           template (profiles/view.html) gated on an is_owner flag.
# Author:   Jamie F. Weleber
# Created:  May 18, 2026
# ===============================================================================
"""Owner-only profile pages — /me (view) and /me/edit (update)."""

import json                                # for re-parsing track GeoJSON before re-dumping the bundle
from pathlib import Path                   # cross-platform path handling

from flask import (Blueprint, abort, current_app, flash, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required

from .models import (User, get_user_stats, list_completions_for_user,
                     list_user_tracks_with_hike, update_user)
from .photo_utils import AVATAR_MAX_EDGE, PHOTO_EXTS, process_photo
from .timeutils import today_az            # Arizona-local "today" for the completion list's status pills

bp = Blueprint("profiles", __name__)


# ===============================================================================
# STEP 1: View — /users/<id> (public) and /me (owner)
# ===============================================================================
# Both views render the same profiles/view.html; the only difference is the
# is_owner flag the template uses to show or hide the "Edit profile" button and
# the per-completion "Edit" links. Every model query already takes a user_id,
# so going public needed no new data layer — just the route below.

def _render_profile(profile_user, is_owner):
    """Gather a user's stats, completions, and tracks and render the shared
    profile template. profile_user is a User object — current_user for /me,
    or the looked-up target for /users/<id>."""
    stats = get_user_stats(profile_user.id)
    completions = list_completions_for_user(profile_user.id)
    tracks = list_user_tracks_with_hike(profile_user.id)

    # Each track's geojson is a JSON string in the DB. Decoding here and
    # bundling into one list lets the template emit a single | tojson — the
    # client then JSON.parses once instead of N+1 times.
    tracks_for_map = []
    for t in tracks:
        tracks_for_map.append({
            "hike_name": t["hike_name"],
            "hike_slug": t["hike_slug"],
            "completed_on": t["completed_on"],
            "geojson": json.loads(t["track_geojson"]),
        })

    return render_template(
        "profiles/view.html",
        profile_user=profile_user,
        is_owner=is_owner,
        stats=stats,
        completions=completions,
        tracks_for_map=tracks_for_map,
        today=today_az().isoformat(),
    )


@bp.route("/users/<int:user_id>")
def view(user_id):
    """Public profile for any user: duck count, aggregated track map, and
    completed-hike list. No login required — profiles are public. The owner-
    only edit controls are suppressed for everyone else (see the template)."""
    profile_user = User.by_id(user_id)
    if profile_user is None:
        abort(404)
    is_owner = current_user.is_authenticated and current_user.id == user_id
    return _render_profile(profile_user, is_owner)


@bp.route("/me")
@login_required
def me():
    """The logged-in user's own profile — the same page as /users/<their id>,
    but reachable without knowing your own id (the nav 'Hi, name' link) and
    with the owner-only edit controls shown."""
    return _render_profile(current_user, is_owner=True)


# ===============================================================================
# STEP 2: Edit — /me/edit
# ===============================================================================

@bp.route("/me/edit", methods=("GET", "POST"))
@login_required
def edit():
    """Owner-only form to change the logged-in user's display name and
    avatar.

    Deliberately scoped — name and avatar only. Email is the login
    identifier and changing it deserves its own re-verification flow;
    password reset is out of scope per CLAUDE.md. Both are fixable
    via direct DB access until / unless those flows get built. is_admin
    isn't editable here either — grant_admin.py remains the only way
    to toggle that, which keeps the privilege escalation path narrow.
    """
    if request.method != "POST":
        return render_template("profiles/edit.html")
    return _save_profile_edit()


def _save_profile_edit():
    """POST handler for edit(). Same validate-then-commit two-phase
    pattern the other write routes use (hikes admin form, completion
    submission)."""
    name = (request.form.get("name") or "").strip()
    remove_avatar = request.form.get("remove_avatar") == "on"
    new_avatar = request.files.get("avatar")

    # ---- Sub-step A: Validate ----
    errors = []
    if not name:
        errors.append("Name is required.")
    if new_avatar and new_avatar.filename:
        ext = Path(new_avatar.filename).suffix.lower()
        if ext not in PHOTO_EXTS:
            errors.append("Avatar must be JPG, PNG, GIF, WEBP, or HEIC.")

    if errors:
        for msg in errors:
            flash(msg, "error")
        return render_template("profiles/edit.html",
                               form_values={"name": name})

    # ---- Sub-step B: Process the new avatar (if any) ----
    avatar_dir = Path(current_app.config["UPLOAD_DIR"]) / "avatars"
    old_avatar = current_user.avatar_filename
    new_avatar_filename = None

    if new_avatar and new_avatar.filename:
        new_avatar.stream.seek(0)
        try:
            # Capped but not thumbnailed, same as the registration path.
            new_avatar_filename, _ = process_photo(new_avatar, avatar_dir,
                                                   max_edge=AVATAR_MAX_EDGE)
        except Exception as exc:
            # If processing fails, re-render the form rather than
            # silently dropping the upload — unlike registration where
            # we proceed without an avatar, here the user explicitly
            # chose to update and would want to know it didn't take.
            flash(f"Could not process avatar: {exc}", "error")
            return render_template("profiles/edit.html",
                                   form_values={"name": name})

    # ---- Sub-step C: Decide the final avatar state ----
    # Upload takes precedence over removal — if the user uploaded a new
    # file AND ticked the remove box, we treat the upload as their
    # actual intent (replace) rather than honoring the contradictory
    # checkbox.
    if new_avatar_filename:
        final_avatar = new_avatar_filename
    elif remove_avatar:
        final_avatar = None
    else:
        final_avatar = old_avatar  # unchanged

    # ---- Sub-step D: Clean up the old file when replacing or removing ----
    # Same disk-housekeeping pattern admin_edit uses when a hike's route
    # file gets replaced — leave no orphaned files behind.
    if old_avatar and final_avatar != old_avatar:
        old_path = avatar_dir / old_avatar
        if old_path.exists():
            old_path.unlink()

    # ---- Sub-step E: Commit ----
    update_user(current_user.id, name=name, avatar_filename=final_avatar)
    flash("Profile updated.", "success")
    return redirect(url_for("profiles.me"))

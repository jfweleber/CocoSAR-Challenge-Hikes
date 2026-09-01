# ===============================================================================
# Module:   app/auth.py
# Purpose:  Flask blueprint for self-registration, login, logout, and
#           password reset. Uses Flask-Login for session management and
#           werkzeug's PBKDF2 password hashing — no custom crypto in
#           this file. Optional avatar upload at registration time goes
#           through the shared photo_utils pipeline so iPhone HEIC
#           selfies get converted to JPEG, same as completion photos.
#
#           Password reset (STEP 4) emails a time-limited token to the
#           registered address; clicking the link in the email lands
#           the user on a "set new password" form. Token plaintext is
#           never stored — only SHA256(token) — so a DB leak can't
#           mint live tokens.
# Author:   Jamie F. Weleber
# Created:  May 18, 2026
# ===============================================================================
"""Account registration, login, logout, password reset."""

import re                                          # email format check (kept deliberately permissive)
from pathlib import Path                           # cross-platform path handling

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from .mail import notify_admin, send_email
from .models import (User, consume_reset_token, create_reset_token,
                     create_user, get_user_by_email, lookup_reset_token,
                     update_password_hash)
from .photo_utils import AVATAR_MAX_EDGE, PHOTO_EXTS, process_photo
from .timeutils import now_az                       # Arizona-local timestamp for the admin notification

bp = Blueprint("auth", __name__, url_prefix="/auth")

# Permissive email regex: "something@something.something" with no
# whitespace or @ in either side. Strict RFC 5322 compliance is famously
# impractical; for a small private team this catches the common typos
# (missing @, missing TLD) without rejecting legitimate addresses.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Minimum password length. Werkzeug's PBKDF2 handles any length safely;
# this is a UX floor against extremely weak passwords like "abc".
MIN_PASSWORD_LEN = 8


# ===============================================================================
# STEP 1: Registration
# ===============================================================================

@bp.route("/register", methods=("GET", "POST"))
def register():
    """Self-registration with name, email, password, optional avatar.

    On success the new user is logged in immediately and redirected
    home — no email verification flow (out of scope per CLAUDE.md;
    add if spam becomes an issue).
    """
    # Already-logged-in users have no business on /register. Bounce
    # them home so an accidental refresh of a register URL doesn't
    # nuke their session.
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method != "POST":
        return render_template("auth/register.html")

    # ---- Read and normalize submitted values ----
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    avatar = request.files.get("avatar")

    # ---- Validate ----
    errors = []
    if not name:
        errors.append("Name is required.")
    if not EMAIL_RE.match(email):
        errors.append("Enter a valid email address.")
    if len(password) < MIN_PASSWORD_LEN:
        errors.append(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    # Short-circuit the DB lookup if cheaper validation already failed —
    # otherwise we'd pointlessly hit the users table on a form that's
    # going to be re-rendered anyway.
    if not errors and get_user_by_email(email):
        errors.append("An account with that email already exists.")

    # Validate avatar extension up front so the error joins the rest;
    # the actual Pillow processing happens after all validation passes.
    if avatar and avatar.filename:
        ext = Path(avatar.filename).suffix.lower()
        if ext not in PHOTO_EXTS:
            errors.append("Avatar must be JPG, PNG, GIF, WEBP, or HEIC.")

    if errors:
        for msg in errors:
            flash(msg, "error")
        # Re-render with the values the user already typed so they don't
        # have to retype everything. Password is deliberately NOT echoed
        # back — common UX rule for any password form.
        return render_template("auth/register.html", name=name, email=email)

    # ---- All checks passed; create the account ----
    # Avatar goes through the shared photo_utils pipeline (HEIC -> JPEG
    # conversion, EXIF auto-rotation). A failure here doesn't block
    # account creation — better to give the user an account with no
    # avatar than to reject them outright; they can upload one from
    # the profile edit page after logging in.
    avatar_filename = None
    if avatar and avatar.filename:
        avatar_dir = Path(current_app.config["UPLOAD_DIR"]) / "avatars"
        avatar.stream.seek(0)
        try:
            # Avatars get the dimension cap but no thumbnail — they're already
            # displayed tiny everywhere, so a second smaller copy would buy
            # nothing. The trailing _ discards the None thumb_filename.
            avatar_filename, _ = process_photo(avatar, avatar_dir,
                                               max_edge=AVATAR_MAX_EDGE)
        except Exception as exc:
            flash(f"Couldn't save your avatar ({exc}); account created "
                  "without one. You can upload one from your profile.",
                  "error")

    user_id = create_user(
        name=name,
        email=email,
        # generate_password_hash uses PBKDF2-SHA256 by default with a
        # per-password salt — werkzeug handles the cryptography; we
        # just never store or transmit plaintext.
        password_hash=generate_password_hash(password),
        avatar_filename=avatar_filename,
    )
    login_user(User.by_id(user_id))

    # Tell the operator somebody signed up. Registration is open to anyone
    # who finds the URL, so this doubles as the tripwire for spam: a burst
    # of these is the signal that the form needs a gate on it. The member's
    # email is included because that's the part that makes a bogus signup
    # recognizable at a glance.
    #
    # notify_admin never raises — see its docstring. A mail problem must
    # not cost somebody the account they just created.
    notify_admin(
        f"New account: {name}",
        f"{name} <{email}> registered at {now_az():%Y-%m-%d %H:%M} (Arizona).\n"
        f"Avatar: {'yes' if avatar_filename else 'no'}\n\n"
        f"{url_for('profiles.view', user_id=user_id, _external=True)}\n"
    )

    flash("Account created. Welcome!", "success")
    return redirect(url_for("index"))


# ===============================================================================
# STEP 2: Login
# ===============================================================================

@bp.route("/login", methods=("GET", "POST"))
def login():
    """Email + password login. On success, honors any ?next= URL set
    by Flask-Login's @login_required redirects, otherwise sends to
    the home page."""
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method != "POST":
        return render_template("auth/login.html")

    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    user = get_user_by_email(email)
    if user and check_password_hash(user.password_hash, password):
        login_user(user)
        next_url = request.args.get("next")
        return redirect(next_url or url_for("index"))

    # Deliberately vague error — doesn't disclose whether the email
    # exists or whether the password was wrong. Both halves are
    # treated the same so an attacker can't enumerate users.
    flash("Invalid email or password.", "error")
    return render_template("auth/login.html", email=email)


# ===============================================================================
# STEP 3: Logout
# ===============================================================================

@bp.route("/logout", methods=("POST",))
@login_required
def logout():
    """POST-only logout. A GET-link logout would be CSRF-able from a
    stray <img src="/auth/logout"> on a malicious page; requiring POST
    + the form button in the nav prevents that."""
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


# ===============================================================================
# STEP 4: Password reset
# ===============================================================================

@bp.route("/forgot", methods=("GET", "POST"))
def forgot():
    """Request a password-reset link by email.

    Always returns the same generic "if that email is registered, a
    link is on its way" flash — regardless of whether the email
    actually exists, whether the SMTP send succeeded, or whether the
    per-user rate limit kicked in. This uniform response is the
    single most important defense against email enumeration via the
    form: an attacker submitting addresses sees identical behavior
    whether or not each one is a real account.
    """
    if current_user.is_authenticated:
        # Logged-in users don't need this flow — they can sign out and
        # log back in with their existing credentials. Bouncing them
        # home avoids spurious token issuance.
        return redirect(url_for("index"))

    if request.method != "POST":
        return render_template("auth/forgot.html")

    email = (request.form.get("email") or "").strip().lower()

    # Only proceed if email is at least syntactically valid AND maps to
    # a real user. Failures are silent — same generic flash either way.
    # create_reset_token() handles per-user rate limiting; a None return
    # means "rate-limited, skip sending."
    if EMAIL_RE.match(email):
        user = get_user_by_email(email)
        if user:
            token = create_reset_token(user.id)
            if token is not None:
                _send_reset_email(user, token)

    flash("If that email is registered, you'll receive a reset link "
          "shortly. The link expires in 1 hour.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/reset/<token>", methods=("GET", "POST"))
def reset(token):
    """Set a new password using a one-time token from a reset email.

    GET validates the token without consuming it (so an expired link
    surfaces an explicit "expired" message rather than a blank form
    the user fills in only to be rejected). POST consumes the token
    atomically — two simultaneous POSTs on the same token can't both
    succeed.
    """
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method != "POST":
        # Non-consuming check — only show the form for valid tokens.
        if lookup_reset_token(token) is None:
            flash("That reset link is invalid or has expired. "
                  "Request a new one if you still need it.", "error")
            return redirect(url_for("auth.forgot"))
        return render_template("auth/reset.html", token=token)

    # Validate password FIRST. If we consumed the token first and then
    # found the password too short, we'd burn the token and force the
    # user to request a new email — bad UX for a fixable typo.
    password = request.form.get("password") or ""
    if len(password) < MIN_PASSWORD_LEN:
        flash(f"Password must be at least {MIN_PASSWORD_LEN} characters.", "error")
        return render_template("auth/reset.html", token=token)

    # Atomic consume — even if the user double-clicks Submit, only
    # one of the two POSTs returns a user_id. The other gets None.
    user_id = consume_reset_token(token)
    if user_id is None:
        flash("That reset link is invalid or has expired. "
              "Request a new one if you still need it.", "error")
        return redirect(url_for("auth.forgot"))

    update_password_hash(user_id, generate_password_hash(password))
    flash("Password updated. You can log in with your new password now.", "success")
    return redirect(url_for("auth.login"))


def _send_reset_email(user, token):
    """Render the reset email template and dispatch it via app/mail.py.

    Kept as a module-private helper so the email-building logic is
    one easy place to find when tweaking the body copy — separate
    from the route flow control.
    """
    # _external=True generates an absolute URL. ProxyFix in __init__.py
    # is what makes the scheme come back as https on prod (Nginx
    # terminates TLS; gunicorn alone would see only http).
    reset_url = url_for("auth.reset", token=token, _external=True)
    body = render_template(
        "auth/reset_email.txt",
        user=user,
        reset_url=reset_url,
    )
    send_email(
        to=user.email,
        subject="[CocoSAR Challenge Hikes] Reset your password",
        body=body,
    )

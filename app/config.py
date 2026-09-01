# ===============================================================================
# Module:   app/config.py
# Purpose:  Configuration objects for local dev and production. The Flask
#           app factory in __init__.py picks one at startup. Subclass
#           BaseConfig to add a new environment (e.g. a future staging
#           config) and only override what differs from the base.
# Author:   Jamie F. Weleber
# Created:  May 18, 2026
# ===============================================================================
"""Dev / Prod configuration objects."""

import os                          # reading SECRET_KEY from the environment (set by systemd in prod)
from pathlib import Path           # cross-platform path handling

# Project root, resolved from this file's location. Works identically on
# Windows (D:\...\Challenge\) and the Linux server
# (/var/www/challenge.example.net/). Resolving once at import time keeps
# the rest of the file readable.
BASE_DIR = Path(__file__).resolve().parent.parent


class BaseConfig:
    """Shared settings. Dev and Prod inherit and override what differs."""

    # SECRET_KEY signs Flask session cookies. The dev fallback is fine for
    # local work, but the production systemd unit MUST export a real
    # value before launching gunicorn — otherwise BaseConfig's fallback
    # is used and session tokens become forgeable, which would let an
    # attacker mint cookies for any user.
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    # SQLite database file. One file holds the whole app's data — users,
    # hikes, completions, and the metadata for uploaded photos/tracks.
    # The actual uploaded binaries live separately under UPLOAD_DIR.
    DATABASE = str(BASE_DIR / "app" / "challenge.db")

    # User-uploaded files. Subdirectories photos/, tracks/, avatars/
    # are created at app startup if missing (see __init__.py STEP 2).
    UPLOAD_DIR = str(BASE_DIR / "app" / "uploads")

    # Upload limits.
    #   - MAX_PHOTO_BYTES caps each individual photo (enforced per-file in
    #     completions.py with a friendly "X MB - limit is 32 MB" message).
    #   - MAX_PHOTOS_PER_COMPLETION caps how many photos one submission carries.
    #   - MAX_CONTENT_LENGTH caps the whole request body (all photos + tracks +
    #     form fields). Sized to hold a full 8-photo batch (8 x 32 MB) plus headroom
    #     for tracks and multipart overhead; anything over gets a 413 before our
    #     route runs, so nginx's client_max_body_size must be at least this large.
    MAX_PHOTO_BYTES = 32 * 1024 * 1024            # 32 MB per photo
    MAX_PHOTOS_PER_COMPLETION = 8
    MAX_CONTENT_LENGTH = 288 * 1024 * 1024        # 8 x 32 MB + headroom

    # SMTP / email — used by the password-reset flow and (in future)
    # completion notifications. All values read from environment so
    # credentials never end up in source control. When SMTP_HOST is
    # None (local dev with no creds) the mail module logs messages
    # instead of attempting to send, so local testing works without
    # any real SMTP setup.
    # CSRF token lifetime. Flask-WTF defaults to expiring tokens after one
    # hour, independent of the session. That default is wrong for this app:
    # the completion form carries up to eight photos and a written comment,
    # and it is filled in by someone who just got off a mountain and is
    # uploading from a phone on a slow connection. Blowing up an hour in
    # would discard a submission representing a real day's work. None means
    # the token stays valid as long as the session that issued it, which is
    # the correct scope -- the token is bound to the session either way, so
    # this weakens nothing that matters.
    WTF_CSRF_TIME_LIMIT = None

    SMTP_HOST = os.environ.get("SMTP_HOST")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    # MAIL_FROM is the address recipients see in the From header. Do not
    # assume it can differ from SMTP_USER: most providers, Proton included,
    # require the From address to match the address the SMTP credential was
    # issued for. A mismatch does not fall back to sending as the alias —
    # it fails authentication outright with a 535, and because send_email()
    # swallows failures by design (see mail.py), it fails silently while the
    # user is still told their message is on its way. Set both to the same
    # address unless you have confirmed your provider permits otherwise.
    MAIL_FROM = os.environ.get("MAIL_FROM", "noreply@example.com")

    # Where operational notifications go: a new member registered, a
    # completion was submitted. Deliberately NOT defaulted — when this is
    # unset (local dev, or prod before it's configured) notify_admin() is
    # a no-op and nothing is sent. Same degrade-quietly posture as
    # SMTP_HOST, so running the app on a laptop never tries to mail
    # anybody. Set it in /etc/challenge.env on prod.
    NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL")


class DevConfig(BaseConfig):
    """Local development on Jamie's Windows machine. DEBUG=True turns on
    the Werkzeug debugger and auto-reload on file change."""
    DEBUG = True


class ProdConfig(BaseConfig):
    """Production on the Linode (Ubuntu 24.04).

    The systemd unit must export SECRET_KEY=<a long random string>
    before launching gunicorn — otherwise BaseConfig's dev fallback
    would silently take effect and sessions would be forgeable.
    """
    DEBUG = False

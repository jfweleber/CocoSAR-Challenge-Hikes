# ===============================================================================
# Module:   app/__init__.py
# Purpose:  Flask application factory and app-level wiring. Builds the
#           configured Flask app, sets up Flask-Login, registers every
#           feature blueprint, and defines the two app-level routes
#           (home page and the user-uploads file server) that don't
#           naturally belong inside a feature module.
# Author:   Jamie F. Weleber
# Created:  May 18, 2026
# ===============================================================================
"""Flask application factory."""

from datetime import date                              # parsing a queued hike's posted_on for the countdown
from pathlib import Path                               # cross-platform path handling (Windows local + Linux prod)

from flask import Flask, abort, render_template, send_from_directory
from flask_login import LoginManager                   # session-based auth: @login_required + current_user
from flask_wtf.csrf import CSRFProtect, CSRFError    # per-session token on every state-changing request
from werkzeug.middleware.proxy_fix import ProxyFix     # honors X-Forwarded-* from Nginx so url_for(_external=True) is https

from . import models                                   # DB plumbing + ORM-lite User class
from .timeutils import today_az                       # Arizona-local "today" for every active-window decision


# ===============================================================================
# Flask-Login setup
# ===============================================================================
# LoginManager is the extension that wires session-based auth into Flask.
# Configuring it at module scope (not inside the factory) is the canonical
# pattern — init_app() down inside create_app() is what actually attaches
# it to the running app. Module-scope config means the user_loader
# decorator below can register against the manager before any app exists.
login_manager = LoginManager()

# login_view tells Flask-Login where to redirect anonymous users who hit
# a @login_required route — uses the blueprint:function syntax. The flash
# message is shown before the redirect; category controls which CSS
# flash- class it picks up (flash-error for prominent visual weight).
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "error"


@login_manager.user_loader
def _load_user(user_id):
    """Resolve a session's user_id (always a string from Flask-Login) back
    to a User object. Flask-Login calls this on every request, so the
    User is always fresh from the DB — an admin grant or password change
    takes effect on the next page load without re-login."""
    return models.User.by_id(int(user_id))


# ===============================================================================
# CSRF protection
# ===============================================================================
# CSRFProtect validates a per-session token on every state-changing request
# (POST/PUT/PATCH/DELETE) and rejects any that arrives without a valid one.
# Same module-scope pattern as login_manager above: declared here, attached to
# the running app by init_app() inside the factory.
#
# Why this is here at all: every form on this site is a plain HTML POST whose
# only proof of identity is the session cookie, and browsers attach that cookie
# to cross-site form submissions automatically. Without a token, any page
# anywhere could submit our forms on behalf of a logged-in member who merely
# visits it -- deleting their completion, rewriting their profile, or, if that
# member is an admin, deleting a hike and every completion cascading off it.
#
# This was a deliberate omission for a long time, on the reasoning that the
# team is small and known. Open-sourcing the app is what changed the input to
# that decision: the precise shape of every form is now public, so the effort
# required to build such a page dropped to nearly nothing.
csrf = CSRFProtect()


def create_app(config=None):
    """Construct and return the Flask app. wsgi.py uses ProdConfig in
    production; the local `flask run` uses the DevConfig default."""
    app = Flask(__name__)

    # Trust the X-Forwarded-* headers Nginx sets when proxying to us.
    # Critical for url_for(_external=True) to generate https:// URLs
    # on prod — Nginx terminates TLS and forwards plain HTTP to
    # gunicorn over the Unix socket, so without this Flask would
    # think it's serving over plain HTTP and generate http:// links
    # in password-reset emails. In local dev there's no proxy and
    # the headers don't exist, so ProxyFix is a no-op there.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # ===========================================================================
    # STEP 1: Load configuration
    # ===========================================================================
    # config can be passed in (tests, prod) or defaulted (dev). Importing
    # DevConfig lazily here means a misconfigured ProdConfig in production
    # never accidentally pulls in DevConfig as a side effect.
    if config is None:
        from .config import DevConfig
        app.config.from_object(DevConfig)
    else:
        app.config.from_object(config)

    # ===========================================================================
    # STEP 2: Ensure upload subdirs exist
    # ===========================================================================
    # uploads/ holds user-generated content split into three buckets.
    # Creating them at app start is idempotent (exist_ok=True) and means
    # the auth/hikes/completions routes can write files without an
    # existence check on every request.
    upload_root = Path(app.config["UPLOAD_DIR"])
    for sub in ("photos", "tracks", "avatars"):
        (upload_root / sub).mkdir(parents=True, exist_ok=True)

    # ===========================================================================
    # STEP 3: Initialize extensions
    # ===========================================================================
    models.init_app(app)         # registers the DB teardown hook
    login_manager.init_app(app)  # attaches Flask-Login to the request cycle
    csrf.init_app(app)           # rejects state-changing requests lacking a valid token

    @app.errorhandler(CSRFError)
    def _csrf_error(error):
        """Explain a rejected token instead of returning a bare 400.

        Nearly every real occurrence of this is innocent: a member left a
        form open across a logout, cleared cookies mid-session, or hit
        Back to a stale tab and resubmitted. Those people need to be told
        to reload and try again, not shown an unexplained error page that
        reads like the site is broken. An actual forged request lands here
        too and is refused identically -- it just doesn't read the page.
        """
        return render_template("csrf_error.html", reason=error.description), 400

    # ===========================================================================
    # STEP 4: Register feature blueprints
    # ===========================================================================
    # Imports happen inside the factory rather than at module top so a
    # subscriber that re-imports `app` doesn't pull in the whole feature
    # surface as a side effect.
    from . import auth, hikes, completions, profiles
    app.register_blueprint(auth.bp)
    app.register_blueprint(hikes.bp)
    app.register_blueprint(completions.bp)
    app.register_blueprint(profiles.bp)

    # ===========================================================================
    # STEP 5: App-level routes
    # ===========================================================================
    # These two routes don't belong inside any feature blueprint:
    # /uploads/... is shared infrastructure for serving user-generated
    # files; / is the landing page that pulls from multiple features.

    @app.route("/uploads/<string:subdir>/<path:filename>")
    def uploaded_file(subdir, filename):
        """Serve a user-uploaded file (photo, track, or avatar).

        Subdir is whitelisted to the three known buckets; anything else
        404s before we touch the filesystem. send_from_directory layers
        on its own path-traversal guard so a crafted ../etc/passwd-style
        filename still can't escape the upload tree.

        In production, Nginx can short-circuit this with a direct
        `alias` to the uploads directory for performance, but the Flask
        route is the canonical fallback and always works in local dev.
        """
        if subdir not in ("photos", "tracks", "avatars"):
            abort(404)
        return send_from_directory(
            str(Path(app.config["UPLOAD_DIR"]) / subdir),
            filename,
        )

    # ===========================================================================
    # STEP 6: Cache-busting
    # ===========================================================================
    # Two complementary hooks that together let users see fresh content
    # after a deploy without needing to know about Ctrl+Shift+R or any
    # browser-specific cache-clearing ritual. url_defaults fingerprints
    # static URLs so changed assets bypass the browser cache; after_request
    # stamps no-cache on HTML so the browser doesn't keep serving stale
    # HTML that still points at old static URLs.

    @app.url_defaults
    def _add_static_cache_buster(endpoint, values):
        """Append `v=<mtime>` to every url_for('static', filename=...).

        Whenever a static file's mtime changes on disk (every FileZilla
        upload bumps it whether or not bytes changed — fine for our
        purposes since "deploy = bump everything" is what we want), the
        generated URL changes, browsers treat it as a new resource, and
        any stale cache no longer applies. Cheap (single stat() call)
        and transparent to template code — every existing
        url_for('static', filename=...) call gets the version added
        automatically with no template changes.

        Missing files get no `v` parameter, falling through to a normal
        404 instead of breaking the page render.
        """
        if endpoint == "static":
            filename = values.get("filename")
            if filename:
                try:
                    mtime = (Path(app.static_folder) / filename).stat().st_mtime
                    values["v"] = int(mtime)
                except OSError:
                    pass

    @app.after_request
    def _no_cache_html(response):
        """Stamp a no-cache header on dynamic HTML responses so the
        browser always asks the server "is this still fresh?" rather
        than blindly serving its cached copy.

        Why this matters alongside static fingerprinting: the cache
        buster above only invalidates static URLs IF the user fetches
        fresh HTML that points to the new URLs. Mobile browsers — Brave
        on Android in particular, observed in prod — can hold a stale
        HTML page indefinitely even through user refreshes. Without
        this header, fingerprinting alone doesn't help that case.

        `no-cache, must-revalidate` doesn't disable caching outright;
        it forces revalidation on each request. Conditional 304
        responses are tiny, so the perf cost is negligible.
        """
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    # How many recent completions the home page's activity strip shows.
    # Twelve fills two comfortable rows of avatar chips on a desktop width
    # and stays scannable on a phone without turning into a wall of faces.
    RECENT_FINISHER_LIMIT = 12

    @app.route("/")
    def index():
        """Site landing page: hero + recent activity + story + leaderboard.

        Hero state machine, decided here so the template doesn't have to:
          A — featured_hike set                → hero with map + CTA
          B — nothing posted yet, one queued   → countdown to the first drop
          C — nothing at all                   → 'being planned' placeholder

        This used to have a fourth state. Back when hikes closed, the site
        spent weeks at a time with no active hike, so the hero fell back to
        a "victory lap" showing the last quarter's finishers. Hikes don't
        close any more, which means that state can never be reached again —
        once anything has been posted, featured_hike is always set. The
        finisher grid was the best part of that block, so rather than
        deleting it we moved it out of the hero and pointed it at recent
        completions across the whole catalog (see recent_finishers below).

        upcoming_hike is fetched unconditionally because it's used in both
        remaining states: as the "Up next" strip beneath state A's hero,
        and as the countdown that IS state B. Date math lives here rather
        than in the template so the template stays rendering-focused.
        """
        today_date = today_az()
        today = today_date.isoformat()

        featured_hike = models.get_featured_hike(today)
        upcoming_hike = models.get_next_hike(today)

        days_until_next = None
        featured_tallies = None

        if featured_hike:
            featured_tallies = models.get_hike_tallies(featured_hike["id"])

        if upcoming_hike:
            days_until_next = (date.fromisoformat(upcoming_hike["posted_on"]) - today_date).days

        recent_finishers = models.list_recent_completions(RECENT_FINISHER_LIMIT)
        leaderboard = models.get_leaderboard()

        return render_template("index.html",
                               featured_hike=featured_hike,
                               featured_tallies=featured_tallies,
                               upcoming_hike=upcoming_hike,
                               days_until_next=days_until_next,
                               recent_finishers=recent_finishers,
                               leaderboard=leaderboard)

    return app

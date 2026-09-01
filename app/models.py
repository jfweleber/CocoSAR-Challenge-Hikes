# ===============================================================================
# Module:   app/models.py
# Purpose:  SQLite schema and query helpers. Holds the canonical SCHEMA_SQL
#           string (single source of truth — also consumed by
#           tools/init_db.py), the per-request DB connection plumbing,
#           and every domain-specific query function the app needs,
#           grouped by table.
# Author:   Jamie F. Weleber
# Created:  May 18, 2026
# ===============================================================================
"""SQLite schema, connection plumbing, and query helpers."""

import hashlib                          # SHA256 hashing for password-reset tokens (we never store plaintext)
import secrets                          # cryptographically-strong random for generating those tokens
import sqlite3                          # Python stdlib SQLite driver; no third-party ORM
from datetime import datetime, timedelta, timezone

from flask import current_app, g        # current_app for config access; g for per-request connection caching
from flask_login import UserMixin       # gives User the methods Flask-Login expects (is_authenticated, get_id, ...)


# ===============================================================================
# Schema
# ===============================================================================
# All DDL lives in one string so tools/init_db.py and the running app share
# the same source of truth. Every CREATE uses IF NOT EXISTS so applying
# this on an existing DB is a no-op.
#
# Design choices worth knowing:
#   - SQLite has no native DATE type. ISO YYYY-MM-DD strings sort
#     lexicographically and compare correctly with =/</> operators, so
#     we use TEXT throughout. A different format (e.g. M/D/YYYY) would
#     have broken this.
#   - email is UNIQUE with COLLATE NOCASE so 'Jamie@x' and 'jamie@x'
#     can't both register.
#   - completions.UNIQUE(user_id, hike_id) is what enforces "one duck per
#     user per hike" — the rule is at the table level, not in the route.
#   - hikes.posted_on is the ONLY date a hike carries. There is deliberately
#     no closing date: once a Challenge is posted it stays open forever, so a
#     member who joins the team in 2027 can still earn the duck for a 2025
#     route. That's the whole point — area familiarization doesn't expire,
#     and a new member shouldn't be locked out of the back catalog.
#   - ON DELETE CASCADE on completions, completion_photos, and
#     completion_tracks means deleting a user or hike automatically
#     cleans up the dependent rows. Requires PRAGMA foreign_keys = ON
#     per connection (see get_db() below) since SQLite defaults this
#     OFF for backward compatibility.
#   - route_geojson and track_geojson are TEXT columns holding the
#     parsed track as JSON. Caching the parsed form means the public
#     hike detail page never re-parses the original GPX/KML on render.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    email           TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash   TEXT    NOT NULL,
    avatar_filename TEXT,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS hikes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    slug            TEXT    NOT NULL UNIQUE,
    notes           TEXT,
    posted_on       TEXT    NOT NULL,   -- the day the Challenge drops; no closing date by design
    route_filename  TEXT    NOT NULL,
    route_format    TEXT    NOT NULL CHECK (route_format IN ('gpx','kml')),
    route_geojson   TEXT    NOT NULL,
    distance_m      REAL,
    elev_gain_m     REAL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_hikes_posted_on ON hikes(posted_on);

CREATE TABLE IF NOT EXISTS completions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    hike_id         INTEGER NOT NULL REFERENCES hikes(id) ON DELETE CASCADE,
    completed_on    TEXT,
    comment         TEXT,
    counts          INTEGER NOT NULL DEFAULT 1,   -- 1 = counts toward ducks/leaderboard; 0 = shown but tallied nowhere (admin flag)
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, hike_id)
);

CREATE INDEX IF NOT EXISTS idx_completions_hike ON completions(hike_id);
CREATE INDEX IF NOT EXISTS idx_completions_user ON completions(user_id);

CREATE TABLE IF NOT EXISTS completion_photos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    completion_id   INTEGER NOT NULL REFERENCES completions(id) ON DELETE CASCADE,
    filename        TEXT    NOT NULL,
    -- Grid-sized copy under uploads/photos/thumbs/. Nullable on purpose: a row
    -- predating the thumbnail work (or one whose thumb generation failed) reads
    -- NULL, and the templates fall back to the full-size file. That fallback is
    -- what makes a FileZilla deploy safe even if the backfill script hasn't run
    -- yet — pages stay correct, they're just heavy until it does.
    thumb_filename  TEXT,
    caption         TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_photos_completion ON completion_photos(completion_id);

CREATE TABLE IF NOT EXISTS completion_tracks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    completion_id   INTEGER NOT NULL REFERENCES completions(id) ON DELETE CASCADE,
    filename        TEXT    NOT NULL,
    format          TEXT    NOT NULL CHECK (format IN ('gpx','kml')),
    track_geojson   TEXT    NOT NULL,
    distance_m      REAL,
    elev_gain_m     REAL,
    recorded_at     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tracks_completion ON completion_tracks(completion_id);

CREATE TABLE IF NOT EXISTS password_resets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash      TEXT    NOT NULL UNIQUE,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    used_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token_hash);
CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets(user_id);
"""


# ===============================================================================
# Connection plumbing
# ===============================================================================

def get_db():
    """Per-request SQLite connection cached on Flask's `g` object.

    Two non-default settings that matter:

      - PRAGMA foreign_keys = ON: SQLite defaults foreign keys OFF per
        connection for backward compatibility. The schema's CASCADE
        clauses depend on FK enforcement being active. Without this,
        deleting a hike would silently leave orphaned completion rows
        pointing at a now-nonexistent hike_id.

      - row_factory = sqlite3.Row: lets callers access columns by name
        (row["user_id"]) instead of integer index. Templates also rely
        on this for the `row.column_name` dot syntax in Jinja.
    """
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    """Teardown hook — Flask calls this at the end of every request,
    pass or fail. Returning the connection to nowhere is fine; SQLite
    cleans up cleanly on close."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_app(app):
    """Register the teardown hook with the Flask app. Called from the
    app factory in __init__.py."""
    app.teardown_appcontext(close_db)


# ===============================================================================
# STEP 1: Users
# ===============================================================================

class User(UserMixin):
    """Flask-Login-compatible user record.

    UserMixin gives us is_authenticated, is_active, is_anonymous, and
    get_id() with sensible defaults. We just need __init__ to populate
    fields from a sqlite3.Row.

    is_admin is bool-converted because SQLite stores it as INTEGER (0/1)
    and `if user.is_admin:` reads better than `if user.is_admin == 1:`.
    """

    def __init__(self, row):
        self.id = row["id"]
        self.name = row["name"]
        self.email = row["email"]
        self.password_hash = row["password_hash"]
        self.avatar_filename = row["avatar_filename"]
        self.is_admin = bool(row["is_admin"])
        self.created_at = row["created_at"]

    @classmethod
    def by_id(cls, user_id):
        """Look up a user by primary key. Called by Flask-Login's
        user_loader on every request, so it has to be cheap — the
        indexed PK lookup is O(log n) on the underlying B-tree."""
        row = get_db().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return cls(row) if row else None


def get_user_by_email(email):
    """Case-insensitive email lookup. The UNIQUE constraint on email
    uses COLLATE NOCASE; this SELECT does the same so the lookup
    matches the storage convention."""
    row = get_db().execute(
        "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,),
    ).fetchone()
    return User(row) if row else None


def create_user(name, email, password_hash, avatar_filename=None, is_admin=False):
    """Insert a new user. The caller has already hashed the password
    with werkzeug.security.generate_password_hash; we never see or
    store the plaintext."""
    db = get_db()
    cur = db.execute(
        """INSERT INTO users (name, email, password_hash, avatar_filename, is_admin)
           VALUES (?, ?, ?, ?, ?)""",
        (name, email, password_hash, avatar_filename, int(bool(is_admin))),
    )
    db.commit()
    return cur.lastrowid


# Mutable user fields. Email and password_hash aren't here — they need
# their own re-verification flows (out of scope for v1.5). is_admin isn't
# here either; grant_admin.py is the only way to toggle that bit, and it
# stays that way to keep the privilege escalation path narrow.
USER_UPDATABLE = {"name", "avatar_filename"}


def update_user(user_id, **fields):
    """Partial update of a user's mutable fields (name, avatar_filename).
    The whitelist check prevents an accidental kwarg name from rewriting
    something we don't want changed through this code path — email,
    password_hash, is_admin, or the created_at audit field."""
    bad = set(fields) - USER_UPDATABLE
    if bad:
        raise ValueError(f"Cannot update fields: {bad}")
    if not fields:
        return
    db = get_db()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    db.execute(
        f"UPDATE users SET {set_clause} WHERE id = ?",
        tuple(fields.values()) + (user_id,),
    )
    db.commit()


# ===============================================================================
# STEP 2: Hikes
# ===============================================================================

# Fields that update_hike() is permitted to change. Identifiers (id) and
# audit fields (created_at) aren't included — changing those would either
# be a no-op or actively destructive. Whitelist is a guard against an
# accidental kwarg name passing straight into a SQL UPDATE.
HIKE_UPDATABLE = {
    "name", "slug", "notes", "posted_on",
    "route_filename", "route_format", "route_geojson",
    "distance_m", "elev_gain_m",
}


def list_hikes():
    """All hikes in the catalog, newest first, each carrying two tallies.

    The two numbers are deliberately separate, and the whole site keeps
    them straight the same way:

      - duck_count       = SUM(counts). What the duck icon means, always.
                           A completion an admin flagged "solid, but no
                           duck" contributes 0.
      - completion_count = COUNT(rows). What the word "completions" or
                           "finishers" means, always. Flagged rows still
                           count as people who did the hike.

    They differ only when a completion has been flagged, which is rare —
    but when it happens the labels should stay honest rather than quietly
    reporting one number under the other's name.

    LEFT (not INNER) JOIN so a hike nobody has done yet still appears in
    the catalog with 0 / 0.
    """
    return get_db().execute(
        """SELECT h.*,
                  COALESCE(SUM(c.counts), 0) AS duck_count,
                  COUNT(c.id)                AS completion_count
           FROM hikes h
           LEFT JOIN completions c ON c.hike_id = h.id
           GROUP BY h.id
           ORDER BY h.posted_on DESC"""
    ).fetchall()


def get_hike_by_id(hike_id):
    return get_db().execute(
        "SELECT * FROM hikes WHERE id = ?", (hike_id,)
    ).fetchone()


def get_hike_by_slug(slug):
    """Public URL lookup. Slugs are unique by schema constraint."""
    return get_db().execute(
        "SELECT * FROM hikes WHERE slug = ?", (slug,)
    ).fetchone()


def get_featured_hike(today_iso):
    """The most recently posted hike that has actually dropped, or None
    if nothing has posted yet.

    This is what the home page puts in the hero. Note what it is NOT:
    it is not "the hike you can do right now," because every posted hike
    is one you can do right now. It's simply the newest one — the thing
    to point at when someone lands on the site. Older Challenges are
    equally open; they just live in the catalog rather than the hero.

    The `posted_on <= today` filter is what keeps a hike scheduled for a
    future reveal out of the hero until its day arrives.
    """
    return get_db().execute(
        """SELECT * FROM hikes
           WHERE posted_on <= ?
           ORDER BY posted_on DESC LIMIT 1""",
        (today_iso,),
    ).fetchone()


def get_next_hike(today_iso):
    """The next hike scheduled to drop (posted_on > today), or None if
    nothing future is queued.

    Powers the "Up next" strip on the home page. Queueing a hike with a
    future posted_on is how you announce a Challenge before revealing it
    — though note the row's route is already in the DB at that point, so
    the detail page will show the map to anyone who guesses the slug. If
    you want a genuine surprise, create the row on reveal day.
    """
    return get_db().execute(
        """SELECT * FROM hikes
           WHERE posted_on > ?
           ORDER BY posted_on ASC LIMIT 1""",
        (today_iso,),
    ).fetchone()


def create_hike(name, slug, notes, posted_on,
                route_filename, route_format, route_geojson,
                distance_m, elev_gain_m):
    db = get_db()
    cur = db.execute(
        """INSERT INTO hikes (name, slug, notes, posted_on,
                              route_filename, route_format, route_geojson,
                              distance_m, elev_gain_m)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, slug, notes, posted_on,
         route_filename, route_format, route_geojson,
         distance_m, elev_gain_m),
    )
    db.commit()
    return cur.lastrowid


def update_hike(hike_id, **fields):
    """Partial update — only the fields passed as kwargs are touched.
    The whitelist check prevents a caller from accidentally rewriting
    created_at or some other non-updatable field through a kwarg typo."""
    bad = set(fields) - HIKE_UPDATABLE
    if bad:
        raise ValueError(f"Cannot update fields: {bad}")
    if not fields:
        return
    db = get_db()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    db.execute(
        f"UPDATE hikes SET {set_clause} WHERE id = ?",
        tuple(fields.values()) + (hike_id,),
    )
    db.commit()


def delete_hike(hike_id):
    """Delete a hike. FK CASCADE clears every dependent row across
    completions, completion_photos, completion_tracks. Files on disk
    are cleaned up by the route handler that calls this — the DB
    doesn't know about the filesystem."""
    db = get_db()
    db.execute("DELETE FROM hikes WHERE id = ?", (hike_id,))
    db.commit()


# ===============================================================================
# STEP 3: Completions, photos, tracks
# ===============================================================================

# Only two fields are mutable after a completion is created. user_id and
# hike_id identify the row (changing them would mean a different row);
# created_at is a server-set audit field.
COMPLETION_UPDATABLE = {"completed_on", "comment"}


def create_completion(user_id, hike_id, completed_on, comment=None):
    db = get_db()
    cur = db.execute(
        """INSERT INTO completions (user_id, hike_id, completed_on, comment)
           VALUES (?, ?, ?, ?)""",
        (user_id, hike_id, completed_on, comment),
    )
    db.commit()
    return cur.lastrowid


def update_completion(completion_id, **fields):
    """Partial update of a completion's metadata. Photos and tracks are
    managed through their own add/delete helpers, not through this."""
    bad = set(fields) - COMPLETION_UPDATABLE
    if bad:
        raise ValueError(f"Cannot update fields: {bad}")
    if not fields:
        return
    db = get_db()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    db.execute(
        f"UPDATE completions SET {set_clause} WHERE id = ?",
        tuple(fields.values()) + (completion_id,),
    )
    db.commit()


def get_completion(completion_id):
    return get_db().execute(
        "SELECT * FROM completions WHERE id = ?", (completion_id,)
    ).fetchone()


def get_completion_for_user_hike(user_id, hike_id):
    """The (at most one) completion this user has for this hike.
    UNIQUE(user_id, hike_id) means this returns at most one row;
    None if the user hasn't earned the duck yet."""
    return get_db().execute(
        "SELECT * FROM completions WHERE user_id = ? AND hike_id = ?",
        (user_id, hike_id),
    ).fetchone()


def list_completions_for_hike(hike_id):
    """All completions for a hike with each user's name and avatar
    joined in. Used by the hike detail page's completion roll AND the
    home page's between-quarters finisher grid. Newest submissions first."""
    return get_db().execute(
        """SELECT c.*,
                  u.name AS user_name,
                  u.avatar_filename AS user_avatar
           FROM completions c
           JOIN users u ON u.id = c.user_id
           WHERE c.hike_id = ?
           ORDER BY c.created_at DESC""",
        (hike_id,),
    ).fetchall()


def get_hike_tallies(hike_id):
    """Both per-hike numbers in one round trip: .completions and .ducks.

    Same convention list_hikes() uses, and for the same reason — the two
    can legitimately disagree when an admin has flagged a completion, and
    a page that shows "5 completions" next to five duck icons when one of
    them earned no duck is quietly lying. Callers pick whichever number
    matches the noun they're about to print.

    Cheap enough to call on every hike-page render; both aggregates come
    off the same index scan.
    """
    return get_db().execute(
        """SELECT COUNT(id)                AS completions,
                  COALESCE(SUM(counts), 0) AS ducks
           FROM completions WHERE hike_id = ?""",
        (hike_id,),
    ).fetchone()


def delete_completion(completion_id):
    """Remove a completion. FK CASCADE clears the linked photo and
    track DB rows; the route handler removes the actual files from
    disk separately."""
    db = get_db()
    db.execute("DELETE FROM completions WHERE id = ?", (completion_id,))
    db.commit()


def set_completion_counts(completion_id, counts):
    """Admin flag for a completion: 1 = counts toward ducks + leaderboard,
    0 = still shown on the hike/profile but tallied nowhere. Lets an instructive
    failed attempt stay visible (track and all) without awarding a duck."""
    db = get_db()
    db.execute(
        "UPDATE completions SET counts = ? WHERE id = ?",
        (1 if counts else 0, completion_id),
    )
    db.commit()


def add_completion_photo(completion_id, filename, thumb_filename=None, caption=None):
    """Record an uploaded photo. thumb_filename defaults to None so a
    caller that can't produce a thumbnail (or a future code path that
    doesn't care) still writes a usable row — the templates fall back
    to the full-size file when it's NULL."""
    db = get_db()
    cur = db.execute(
        """INSERT INTO completion_photos (completion_id, filename, thumb_filename, caption)
           VALUES (?, ?, ?, ?)""",
        (completion_id, filename, thumb_filename, caption),
    )
    db.commit()
    return cur.lastrowid


def get_photo(photo_id):
    return get_db().execute(
        "SELECT * FROM completion_photos WHERE id = ?", (photo_id,)
    ).fetchone()


def get_photos_for_completion(completion_id):
    return get_db().execute(
        """SELECT * FROM completion_photos
           WHERE completion_id = ?
           ORDER BY id""",
        (completion_id,),
    ).fetchall()


def delete_photo(photo_id):
    db = get_db()
    db.execute("DELETE FROM completion_photos WHERE id = ?", (photo_id,))
    db.commit()


def add_completion_track(completion_id, filename, fmt, geojson,
                         distance_m=None, elev_gain_m=None, recorded_at=None):
    db = get_db()
    cur = db.execute(
        """INSERT INTO completion_tracks
           (completion_id, filename, format, track_geojson,
            distance_m, elev_gain_m, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (completion_id, filename, fmt, geojson,
         distance_m, elev_gain_m, recorded_at),
    )
    db.commit()
    return cur.lastrowid


def get_track(track_id):
    return get_db().execute(
        "SELECT * FROM completion_tracks WHERE id = ?", (track_id,)
    ).fetchone()


def get_tracks_for_completion(completion_id):
    return get_db().execute(
        """SELECT * FROM completion_tracks
           WHERE completion_id = ?
           ORDER BY id""",
        (completion_id,),
    ).fetchall()


def delete_track(track_id):
    db = get_db()
    db.execute("DELETE FROM completion_tracks WHERE id = ?", (track_id,))
    db.commit()


# ===============================================================================
# STEP 4: Profile queries
# ===============================================================================
# Three helpers that drive the /me page. Each joins through completions
# to either hikes (for the list) or completion_tracks (for the map),
# scoped to one user_id.

def list_completions_for_user(user_id):
    """Every completion a user has earned, newest-first, with the hike's
    name, slug, active window, and headline stats joined in for display."""
    return get_db().execute(
        """SELECT c.*,
                  h.name AS hike_name,
                  h.slug AS hike_slug,
                  h.posted_on,
                  h.distance_m AS hike_distance_m,
                  h.elev_gain_m AS hike_elev_gain_m
           FROM completions c
           JOIN hikes h ON h.id = c.hike_id
           WHERE c.user_id = ?
           ORDER BY c.completed_on DESC""",
        (user_id,),
    ).fetchall()


def list_user_tracks_with_hike(user_id):
    """Every track row a user has uploaded, with hike name + slug + the
    completion date joined in. Drives the consolidated track map on the
    profile page — one polyline per row."""
    return get_db().execute(
        """SELECT ct.*,
                  h.name AS hike_name,
                  h.slug AS hike_slug,
                  c.completed_on
           FROM completion_tracks ct
           JOIN completions c ON c.id = ct.completion_id
           JOIN hikes h ON h.id = c.hike_id
           WHERE c.user_id = ?
           ORDER BY c.completed_on DESC""",
        (user_id,),
    ).fetchall()


def get_user_stats(user_id):
    """Aggregate header stats for a user's profile page.

    Three numbers in one query:
      - duck_count:        counting completions (counts=1) for the user
      - total_distance_m:  SUM of distance_m across all their tracks
      - total_elev_gain_m: SUM of elev_gain_m likewise

    duck_count uses SUM(counts) so a flagged "doesn't count" completion is
    excluded. Distance and elevation, by contrast, reflect what the user
    actually hiked, so a flagged attempt's track miles still count there
    (they did the work — "solid, but no duck"). COALESCE wraps each SUM so
    the SELECT returns 0 instead of NULL when there are no rows.
    """
    return get_db().execute(
        """SELECT
              (SELECT COALESCE(SUM(counts), 0) FROM completions WHERE user_id = :uid)
                AS duck_count,
              (SELECT COALESCE(SUM(ct.distance_m), 0)
               FROM completion_tracks ct
               JOIN completions c ON c.id = ct.completion_id
               WHERE c.user_id = :uid)
                AS total_distance_m,
              (SELECT COALESCE(SUM(ct.elev_gain_m), 0)
               FROM completion_tracks ct
               JOIN completions c ON c.id = ct.completion_id
               WHERE c.user_id = :uid)
                AS total_elev_gain_m""",
        {"uid": user_id},
    ).fetchone()


def list_recent_completions(limit=12):
    """The newest completions across the whole catalog, for the home page.

    This replaces the old "victory lap" block, which only had something to
    show during the gap between one quarter's hike closing and the next
    one opening. With every Challenge permanently open there is no such
    gap, so that block would have gone dark forever. Reading site-wide
    instead of per-hike keeps faces on the landing page continuously —
    and it puts back-catalog work in the spotlight, which is exactly the
    behavior the always-open change is meant to encourage.

    Ordered by completed_on (the day they actually hiked it), not created_at
    (the day they got around to filling in the form). The section is meant to
    highlight recent EFFORT, and those two dates usually agree — but when they
    don't, the hike is the thing worth surfacing. Someone who hiked in June and
    submitted in August has done a recent bit of paperwork, not a recent hike.

    Note this doesn't undercut back-catalog work, which is the behavior the
    always-open change exists to encourage: a member who hikes a 2025 route
    today still sorts to the top, because their completed_on IS today. Only
    late submissions sink.

    created_at breaks ties, so two people who hiked the same day appear in the
    order they submitted, and c.id breaks that in turn for the pathological
    case of two rows written in the same second.

    completed_on is nullable in the schema. SQLite sorts NULLs last under
    DESC, so a legacy row without a date falls off the end of the list rather
    than squatting at the top — which is the behavior we'd want anyway.

    Flagged "no duck" completions (counts = 0) are excluded. The section is
    titled "Recently earned" and shows the duck-bearing chips, so including a
    completion that earned nothing would contradict the site-wide rule that
    duck language means SUM(counts). Those attempts are still visible on the
    hike page, the submitter's profile, and their own completion page — this
    is the one surface where the heading makes a claim they don't satisfy.
    """
    return get_db().execute(
        """SELECT c.id,
                  c.completed_on,
                  c.counts,
                  u.id              AS user_id,
                  u.name            AS user_name,
                  u.avatar_filename AS user_avatar,
                  h.name            AS hike_name,
                  h.slug            AS hike_slug
           FROM completions c
           JOIN users u ON u.id = c.user_id
           JOIN hikes h ON h.id = c.hike_id
           WHERE c.counts = 1
           ORDER BY c.completed_on DESC, c.created_at DESC, c.id DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


# ===============================================================================
# STEP 5: Leaderboard
# ===============================================================================

def get_leaderboard():
    """Team-wide duck tally for the home page.

    Returns a list of dicts (not Rows) because we compute a 'rank' field
    in Python on top of the DB query. Each dict has user_id, name,
    avatar_filename, duck_count, and rank.

    Ranking is "standard competition" style: ties share a rank, and the
    next rank skips by the number of tied entries. So a 5-4-4-3 sequence
    yields ranks 1, 2, 2, 4 — two silvers means no bronze, and bronze
    goes to the next distinct score. This matches the Olympic medal
    convention Jamie called for: tied for silver = both silver.

    Users with zero counting completions don't appear at all: the INNER
    JOIN drops users with no completions, and the HAVING drops anyone
    whose only completions are flagged "doesn't count" (SUM(counts) = 0).
    """
    rows = get_db().execute(
        # SUM(c.counts) is the duck tally — a flagged completion (counts=0) adds
        # nothing. HAVING then hides users left with zero ducks.
        """SELECT u.id            AS user_id,
                  u.name,
                  u.avatar_filename,
                  SUM(c.counts)   AS duck_count
           FROM users u
           JOIN completions c ON c.user_id = u.id
           GROUP BY u.id, u.name, u.avatar_filename
           HAVING SUM(c.counts) > 0
           ORDER BY duck_count DESC, u.name COLLATE NOCASE ASC"""
    ).fetchall()

    # Walk the (already-sorted) rows assigning ranks. Each new distinct
    # duck count opens a new rank slot at position (i); rows tied with
    # the previous reuse prev_rank without advancing.
    results = []
    prev_count = None
    prev_rank = 0
    for i, row in enumerate(rows, start=1):
        count = row["duck_count"]
        if count == prev_count:
            rank = prev_rank
        else:
            rank = i
            prev_count = count
            prev_rank = rank
        results.append({
            "user_id": row["user_id"],
            "name": row["name"],
            "avatar_filename": row["avatar_filename"],
            "duck_count": count,
            "rank": rank,
        })
    return results


# ===============================================================================
# STEP 6: Password resets
# ===============================================================================
# Tokens live in the password_resets table. We store SHA256(token), never
# the plaintext — if the DB ever leaks, the stored hashes can't be used
# to mint or replay live tokens. used_at marks consumption so a token
# can't be replayed even by someone who intercepts the email link.

PASSWORD_RESET_TTL = timedelta(hours=1)               # link lifetime
PASSWORD_RESET_RATE_LIMIT = timedelta(minutes=5)      # min gap between requests per user


def _datetime_offset(delta):
    """Return a SQLite-comparable UTC datetime string offset from now.

    SQLite stores datetime('now') as naive UTC in the format
    'YYYY-MM-DD HH:MM:SS'. We compute the offset in tz-aware UTC
    then strip the suffix so the string-comparison cutoffs in the
    queries below line up with the stored values exactly.
    """
    aware = datetime.now(timezone.utc) + delta
    return aware.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def create_reset_token(user_id):
    """Generate a one-time password-reset token for a user.

    Returns the plaintext token (to embed in the email link), or
    None if rate-limiting kicked in — meaning a token was already
    issued for this user within the last 5 minutes. The forgot
    route treats None as "silently skip the send"; the user sees
    the same generic success flash either way, which closes the
    timing channel an attacker could use to detect rate-limiting.

    secrets.token_urlsafe(32) gives ~43 chars of URL-safe base64
    entropy — way more than we need but trivially cheap.
    """
    db = get_db()

    # Rate limit: bail if any token was created for this user inside
    # the last PASSWORD_RESET_RATE_LIMIT window. Closes the
    # mail-flood attack vector.
    recent = db.execute(
        """SELECT 1 FROM password_resets
           WHERE user_id = ?
             AND created_at >= ?""",
        (user_id, _datetime_offset(-PASSWORD_RESET_RATE_LIMIT)),
    ).fetchone()
    if recent:
        return None

    # Housekeeping: prune tokens older than 24 hours for this user
    # so the table doesn't accumulate forever. Anything still inside
    # the TTL window is kept (might still be active).
    db.execute(
        """DELETE FROM password_resets
           WHERE user_id = ?
             AND created_at < ?""",
        (user_id, _datetime_offset(-timedelta(hours=24))),
    )

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db.execute(
        """INSERT INTO password_resets (user_id, token_hash)
           VALUES (?, ?)""",
        (user_id, token_hash),
    )
    db.commit()
    return token


def lookup_reset_token(token):
    """Return user_id if the token is valid (exists, not used, not
    expired). Does NOT mark the token as used.

    Used by the GET on /reset/<token> to decide whether to render
    the form or an "invalid/expired link" message. Separating
    lookup from consume means we don't burn a valid token just
    because someone clicked the link to verify it.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    cutoff = _datetime_offset(-PASSWORD_RESET_TTL)
    row = get_db().execute(
        """SELECT user_id FROM password_resets
           WHERE token_hash = ?
             AND used_at IS NULL
             AND created_at >= ?""",
        (token_hash, cutoff),
    ).fetchone()
    return row["user_id"] if row else None


def consume_reset_token(token):
    """Mark a token as used and return its user_id, atomically.

    Returns None if the token doesn't exist, has expired, or has
    already been used. The conditional UPDATE...RETURNING ensures
    that two simultaneous POSTs on the same token can't both
    succeed — exactly one will get the user_id back, the other
    will get None. No application-level locking needed.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    cutoff = _datetime_offset(-PASSWORD_RESET_TTL)
    db = get_db()
    cur = db.execute(
        """UPDATE password_resets
           SET used_at = datetime('now')
           WHERE token_hash = ?
             AND used_at IS NULL
             AND created_at >= ?
           RETURNING user_id""",
        (token_hash, cutoff),
    )
    row = cur.fetchone()
    db.commit()
    return row["user_id"] if row else None


def update_password_hash(user_id, password_hash):
    """Update a user's password hash. Used by the reset flow today
    and is the natural API for a future logged-in change-password
    feature too."""
    db = get_db()
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (password_hash, user_id),
    )
    db.commit()

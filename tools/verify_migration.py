# ===============================================================================
# Script Name:  tools/verify_migration.py
# Purpose:      Throwaway end-to-end check for the always-open Challenge change.
#               Builds a database in the OLD schema (active_from / active_to,
#               no thumb_filename) seeded to look like production, runs both
#               migration scripts against it, then drives every route with the
#               Flask test client to confirm nothing 500s and the new rules
#               actually hold.
#
#               Not part of the running app and never imported by it — it only
#               ever touches a scratch database in a temp directory, so it is
#               safe to leave in place. Run it after any change to the hike
#               lifecycle, the completion date rules, or the photo pipeline.
#
# Usage:        From the project root:  python tools/verify_migration.py
# ===============================================================================

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # project root, one level up from tools/
sys.path.insert(0, str(ROOT))

from PIL import Image

# ---------------------------------------------------------------------------
# The pre-migration schema, copied verbatim from git-less history (i.e. from
# the version of models.py this change replaced). Reproducing it here rather
# than importing SCHEMA_SQL is the entire point: we need to prove the
# migration moves a REAL old database, not that it's a no-op on a new one.
# ---------------------------------------------------------------------------
OLD_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
    avatar_filename TEXT, is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE hikes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE, notes TEXT,
    active_from TEXT NOT NULL, active_to TEXT NOT NULL,
    route_filename TEXT NOT NULL,
    route_format TEXT NOT NULL CHECK (route_format IN ('gpx','kml')),
    route_geojson TEXT NOT NULL, distance_m REAL, elev_gain_m REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE INDEX idx_hikes_active_from ON hikes(active_from);
CREATE INDEX idx_hikes_active_to   ON hikes(active_to);
CREATE TABLE completions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    hike_id INTEGER NOT NULL REFERENCES hikes(id) ON DELETE CASCADE,
    completed_on TEXT, comment TEXT, counts INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, hike_id));
CREATE TABLE completion_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    completion_id INTEGER NOT NULL REFERENCES completions(id) ON DELETE CASCADE,
    filename TEXT NOT NULL, caption TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE completion_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    completion_id INTEGER NOT NULL REFERENCES completions(id) ON DELETE CASCADE,
    filename TEXT NOT NULL, format TEXT NOT NULL CHECK (format IN ('gpx','kml')),
    track_geojson TEXT NOT NULL, distance_m REAL, elev_gain_m REAL,
    recorded_at TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE password_resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')), used_at TEXT);
"""

GEOJSON = ('{"type":"Feature","geometry":{"type":"LineString","coordinates":'
           '[[-111.65,35.19,2100.0],[-111.66,35.20,2250.0],[-111.67,35.21,2180.0]]},'
           '"properties":{}}')

GPX = """<?xml version="1.0"?>
<gpx version="1.1" creator="verify"><trk><trkseg>
<trkpt lat="35.19" lon="-111.65"><ele>2100</ele></trkpt>
<trkpt lat="35.20" lon="-111.66"><ele>2250</ele></trkpt>
<trkpt lat="35.21" lon="-111.67"><ele>2180</ele></trkpt>
</trkseg></trk></gpx>"""

PASSES = []
FAILS = []


def check(label, condition, detail=""):
    (PASSES if condition else FAILS).append(label)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  — {detail}" if detail and not condition else ""))


def build_old_db(db_path, uploads):
    """Seed a pre-migration database plus matching files on disk."""
    for sub in ("photos", "tracks", "avatars"):
        (uploads / sub).mkdir(parents=True, exist_ok=True)

    # Two oversized photos so the resize step has something real to chew on:
    # a JPEG and an RGBA PNG (the transparency-flattening path).
    Image.new("RGB", (4000, 3000), (90, 120, 70)).save(uploads / "photos" / "big.jpg", quality=95)
    Image.new("RGBA", (3200, 2400), (10, 40, 80, 128)).save(uploads / "photos" / "alpha.png")
    Image.new("RGB", (2000, 2000), (200, 40, 40)).save(uploads / "avatars" / "face.jpg")
    (uploads / "tracks" / "route.gpx").write_text(GPX)
    (uploads / "tracks" / "mine.gpx").write_text(GPX)

    con = sqlite3.connect(db_path)
    con.executescript(OLD_SCHEMA)
    from werkzeug.security import generate_password_hash
    pw = generate_password_hash("hunter2hunter2")
    con.execute("INSERT INTO users (name,email,password_hash,avatar_filename,is_admin) "
                "VALUES ('Jamie Weleber','jamie@example.com',?, 'face.jpg', 1)", (pw,))
    con.execute("INSERT INTO users (name,email,password_hash,is_admin) "
                "VALUES ('New Member','newbie@example.com',?, 0)", (pw,))
    # Holds the flagged completion, so the new member keeps a clean slate and
    # can be used to exercise the submit route's validation on any hike.
    con.execute("INSERT INTO users (name,email,password_hash,is_admin) "
                "VALUES ('Flagged Member','flagged@example.com',?, 0)", (pw,))
    # An old closed hike (the case this whole change is about), a current one,
    # and one queued for a future reveal.
    for name, slug, af, at in (
        ("Horseshoe Mesa", "horseshoe-mesa", "2025-01-01", "2025-03-31"),
        ("Blue Dot O' Fun", "blue-dot-oldham", "2026-06-01", "2026-08-31"),
        ("Future Route", "future-route", "2026-12-01", "2027-02-28"),
    ):
        con.execute(
            "INSERT INTO hikes (name,slug,notes,active_from,active_to,route_filename,"
            "route_format,route_geojson,distance_m,elev_gain_m) "
            "VALUES (?,?,'Notes here',?,?,'route.gpx','gpx',?,12862.0,731.0)",
            (name, slug, af, at, GEOJSON))
    con.execute("INSERT INTO completions (user_id,hike_id,completed_on,comment) "
                "VALUES (1,1,'2025-03-06','Good day out')")
    # A flagged completion, so the duck-vs-completion split is exercised.
    con.execute("INSERT INTO completions (user_id,hike_id,completed_on,counts) "
                "VALUES (3,1,'2025-03-20',0)")
    con.execute("INSERT INTO completions (user_id,hike_id,completed_on) "
                "VALUES (1,2,'2026-06-05')")
    con.execute("INSERT INTO completion_photos (completion_id,filename) VALUES (1,'big.jpg')")
    con.execute("INSERT INTO completion_photos (completion_id,filename) VALUES (1,'alpha.png')")
    # A row whose file is missing on disk — the backfill must survive it.
    con.execute("INSERT INTO completion_photos (completion_id,filename) VALUES (3,'gone.jpg')")
    con.execute("INSERT INTO completion_tracks (completion_id,filename,format,track_geojson,"
                "distance_m,elev_gain_m) VALUES (1,'mine.gpx','gpx',?,12862.0,731.0)", (GEOJSON,))
    con.commit()
    con.close()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="challenge-verify-"))
    db_path = tmp / "challenge.db"
    uploads = tmp / "uploads"
    print(f"Scratch dir: {tmp}\n")

    print("STEP 1 — build a pre-migration database")
    build_old_db(str(db_path), uploads)
    cols = [r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(hikes)")]
    check("old schema has active_to", "active_to" in cols)

    print("\nSTEP 2 — run migrate_open_hikes.py")
    import subprocess
    for args in (["tools/migrate_open_hikes.py", "--db", str(db_path)],
                 # Second run proves idempotency.
                 ["tools/migrate_open_hikes.py", "--db", str(db_path), "--no-backup"]):
        r = subprocess.run([sys.executable] + args, cwd=ROOT, capture_output=True, text=True)
        print("   " + r.stdout.strip().replace("\n", "\n   "))
        if r.returncode != 0:
            print(r.stderr)
    con = sqlite3.connect(db_path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(hikes)")]
    check("active_to dropped", "active_to" not in cols)
    check("posted_on present", "posted_on" in cols)
    check("posted_on kept the old active_from values",
          con.execute("SELECT posted_on FROM hikes WHERE slug='horseshoe-mesa'").fetchone()[0]
          == "2025-01-01")
    idx = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='hikes'")]
    check("idx_hikes_posted_on exists", "idx_hikes_posted_on" in idx, str(idx))
    check("stale hike indexes gone",
          not any(n and "active" in n for n in idx), str(idx))
    pcols = [r[1] for r in con.execute("PRAGMA table_info(completion_photos)")]
    check("thumb_filename added", "thumb_filename" in pcols)
    check("completions survived migration",
          con.execute("SELECT COUNT(*) FROM completions").fetchone()[0] == 3)
    check("a .bak was written", any(p.name.endswith(".bak") for p in tmp.iterdir()))
    con.close()

    print("\nSTEP 3 — run migrate_thumbnails.py (with --resize-originals)")
    r = subprocess.run([sys.executable, "tools/migrate_thumbnails.py",
                        "--db", str(db_path), "--uploads", str(uploads),
                        "--resize-originals"],
                       cwd=ROOT, capture_output=True, text=True)
    print("   " + (r.stdout or r.stderr).strip().replace("\n", "\n   "))
    thumbs = uploads / "photos" / "thumbs"
    check("thumbs directory created", thumbs.is_dir())
    check("thumbnail generated for the JPEG", (thumbs / "big.jpg").exists())
    check("thumbnail generated for the RGBA PNG", (thumbs / "alpha.jpg").exists())
    if (thumbs / "big.jpg").exists():
        check("thumbnail is <= 480px", max(Image.open(thumbs / "big.jpg").size) <= 480)
    check("original photo capped at 2560px",
          max(Image.open(uploads / "photos" / "big.jpg").size) <= 2560)
    check("avatar capped at 512px",
          max(Image.open(uploads / "avatars" / "face.jpg").size) <= 512)
    con = sqlite3.connect(db_path)
    check("thumb_filename written back to the DB",
          con.execute("SELECT thumb_filename FROM completion_photos WHERE filename='big.jpg'"
                      ).fetchone()[0] == "big.jpg")
    check("missing-file row left NULL, not crashed",
          con.execute("SELECT thumb_filename FROM completion_photos WHERE filename='gone.jpg'"
                      ).fetchone()[0] is None)
    con.close()

    print("\nSTEP 4 — boot the app and exercise every route")
    from app import create_app
    from app.config import BaseConfig

    class TestConfig(BaseConfig):
        DATABASE = str(db_path)
        UPLOAD_DIR = str(uploads)
        SECRET_KEY = "verify-only"
        TESTING = True
        WTF_CSRF_ENABLED = False

    app = create_app(TestConfig)
    c = app.test_client()

    for label, url in (("home", "/"), ("catalog", "/hikes"),
                       ("closed-era hike detail", "/hikes/horseshoe-mesa"),
                       ("current hike detail", "/hikes/blue-dot-oldham"),
                       ("queued hike detail", "/hikes/future-route"),
                       ("public profile", "/users/1"),
                       ("completion page", "/completions/1"),
                       ("login", "/auth/login"), ("register", "/auth/register"),
                       ("forgot", "/auth/forgot"),
                       ("thumbnail file", "/uploads/photos/thumbs/big.jpg")):
        resp = c.get(url)
        check(f"GET {label} -> 200", resp.status_code == 200, f"got {resp.status_code}")

    body = c.get("/").get_data(as_text=True)
    # Jinja escapes the apostrophe, so match the escaped form.
    check("home hero features the newest posted hike", "Blue Dot O&#39; Fun" in body)
    check("home says Challenges stay open", "open indefinitely" in body)
    check("recent finishers section rendered", "Recently earned" in body)
    check("no leftover countdown wording", "days left" not in body)

    body = c.get("/hikes").get_data(as_text=True)
    check("catalog labels the newest hike", ">newest<" in body)
    check("catalog labels older hikes open, not past", ">open<" in body and ">past<" not in body)
    check("catalog labels the queued hike upcoming", ">upcoming<" in body)

    body = c.get("/hikes/horseshoe-mesa").get_data(as_text=True)
    check("old hike shows a posted date", "Posted 2025-01-01" in body)
    check("old hike duck/completion split shown", "1 duck awarded" in body)

    # --- Log in as the new member and complete a two-year-old hike ---
    r = c.post("/auth/login", data={"email": "newbie@example.com",
                                    "password": "hunter2hunter2"},
               follow_redirects=True)
    check("login works", r.status_code == 200)

    body = c.get("/hikes/blue-dot-oldham").get_data(as_text=True)
    check("open hike offers submission", "I completed this hike" in body)
    check("open hike states there's no deadline", "No deadline" in body)

    body = c.get("/hikes/future-route").get_data(as_text=True)
    check("queued hike blocks submission", "This Challenge drops on" in body)

    r = c.get("/hikes/future-route/complete", follow_redirects=True)
    check("submit route rejects a queued hike", "This Challenge drops on" in
          r.get_data(as_text=True))

    # THE point of the change: a member completing a Challenge posted long ago.
    import io
    photo = io.BytesIO()
    Image.new("RGB", (3600, 2400), (30, 90, 140)).save(photo, "JPEG")
    photo.seek(0)
    r = c.post("/hikes/blue-dot-oldham/complete",
               data={"completed_on": "2026-08-20", "comment": "Backfilled years later",
                     "photos": (photo, "summit.jpg"),
                     "tracks": (io.BytesIO(GPX.encode()), "track.gpx")},
               content_type="multipart/form-data", follow_redirects=True)
    body = r.get_data(as_text=True)
    check("completion accepted long after the old window closed",
          "earned the duck" in body, body[-400:])

    con = sqlite3.connect(db_path)
    row = con.execute("SELECT filename, thumb_filename FROM completion_photos "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    check("new upload recorded a thumbnail", row[1] is not None, str(row))
    if row[1]:
        check("new upload's thumbnail exists on disk", (thumbs / row[1]).exists())
        check("new upload's stored copy was capped",
              max(Image.open(uploads / "photos" / row[0]).size) <= 2560)
    con.close()

    # --- Date-rule boundaries ---
    r = c.post("/hikes/horseshoe-mesa/complete",
               data={"completed_on": "2024-12-31", "comment": "before it existed",
                     "photos": (io.BytesIO(b"x"), ""),
                     "tracks": (io.BytesIO(GPX.encode()), "t.gpx")},
               content_type="multipart/form-data", follow_redirects=True)
    check("date before posting is rejected",
          "before this Challenge was" in r.get_data(as_text=True))

    r = c.post("/hikes/horseshoe-mesa/complete",
               data={"completed_on": "2099-01-01",
                     "tracks": (io.BytesIO(GPX.encode()), "t.gpx")},
               content_type="multipart/form-data", follow_redirects=True)
    check("future date is rejected",
          "be in the future" in r.get_data(as_text=True))

    # --- Admin surfaces ---
    c.post("/auth/logout")
    c.post("/auth/login", data={"email": "jamie@example.com", "password": "hunter2hunter2"})
    for label, url in (("admin list", "/admin/hikes"), ("admin new", "/admin/hikes/new"),
                       ("admin edit", "/admin/hikes/1/edit"), ("own profile", "/me"),
                       ("profile edit", "/me/edit"),
                       ("completion edit", "/completions/1/edit")):
        resp = c.get(url)
        check(f"GET {label} -> 200", resp.status_code == 200, f"got {resp.status_code}")

    # Creating a hike through the admin form is the last untested write path.
    r = c.post("/admin/hikes/new",
               data={"name": "Brand New Route", "slug": "", "notes": "n",
                     "posted_on": "2026-08-01",
                     "route": (io.BytesIO(GPX.encode()), "r.gpx")},
               content_type="multipart/form-data", follow_redirects=True)
    check("admin can create a hike with only a posted date",
          "Brand New Route" in r.get_data(as_text=True))
    r = c.get("/")
    check("newly posted hike takes over the hero",
          "Brand New Route" in r.get_data(as_text=True))

    # ---- Track parsing: de-duplication and gain hysteresis ----
    # These are unit checks on track_parser, not route checks, but they live
    # here so one command covers everything that can silently corrupt a
    # hike's headline numbers.
    from app.track_parser import ELEV_GAIN_THRESHOLD_M, parse_track

    def gpx_of(points):
        """Build a minimal GPX from (lat, lon, ele) triples."""
        body = "".join(
            f'<trkpt lat="{la}" lon="{lo}"><ele>{el}</ele></trkpt>'
            for la, lo, el in points)
        return (b'<?xml version="1.0"?><gpx version="1.1" creator="verify">'
                b'<trk><trkseg>' + body.encode() + b'</trkseg></trk></gpx>')

    # A short climb where every point is emitted three times, the way the
    # Esri export does it. De-dupe must strip the repeats without moving
    # either headline number.
    walked = [(35.20 + i * 0.001, -111.60, 2000 + i * 10) for i in range(20)]
    tripled = [pt for pt in walked for _ in range(3)]
    geo_clean, d_clean, g_clean = parse_track(gpx_of(walked), "gpx")
    geo_dup, d_dup, g_dup = parse_track(gpx_of(tripled), "gpx")
    check("de-dupe strips repeated positions",
          len(geo_dup["geometry"]["coordinates"]) == len(walked),
          f'{len(geo_dup["geometry"]["coordinates"])} coords from {len(tripled)} points')
    check("de-dupe leaves distance identical", abs(d_dup - d_clean) < 1e-6)
    check("de-dupe leaves gain identical", abs(g_dup - g_clean) < 1e-6)

    # A file that is one position repeated must fail as an empty track
    # rather than being accepted as a zero-length one.
    try:
        parse_track(gpx_of([(35.2, -111.6, 2000)] * 50), "gpx")
        check("an all-duplicate file is rejected", False, "it parsed without error")
    except ValueError:
        check("an all-duplicate file is rejected", True)

    # Hysteresis: a steady climb carrying sub-threshold sawtooth noise.
    # The naive sum would bank every tooth; the threshold should report
    # close to the true climb instead.
    noisy, elev = [], 2000.0
    for i in range(400):
        elev += 1.0                                   # 1 m of real climb per step
        jitter = 0.8 if i % 2 else -0.8               # under ELEV_GAIN_THRESHOLD_M
        noisy.append((35.20 + i * 0.0002, -111.60, elev + jitter))
    _, _, g_noisy = parse_track(gpx_of(noisy), "gpx")
    true_climb = noisy[-1][2] - noisy[0][2]
    naive = sum(max(0, noisy[i][2] - noisy[i - 1][2]) for i in range(1, len(noisy)))

    # Measure how much of the naive sum's ERROR was removed, rather than
    # demanding some fraction of the naive total — the correct answer here
    # is the true climb, and an earlier version of this check asked the
    # algorithm to undershoot it.
    removed = (naive - g_noisy) / (naive - true_climb)
    check("hysteresis removes the jitter the naive sum banks",
          removed > 0.9,
          f"removed {removed:.0%} of the {naive - true_climb:.0f} m error "
          f"(naive {naive:.0f}, thresholded {g_noisy:.0f}, true {true_climb:.0f})")
    check("hysteresis stays close to the real climb",
          abs(g_noisy - true_climb) < ELEV_GAIN_THRESHOLD_M * 2,
          f"{g_noisy:.0f} m vs true {true_climb:.0f} m")

    # A genuine dip must still count fully on the way back out — that is the
    # difference between hysteresis and a blanket smoothing pass.
    dip = ([(35.20 + i * 0.001, -111.60, 2000 - i * 20) for i in range(6)] +
           [(35.205 + i * 0.001, -111.60, 1900 + i * 20) for i in range(1, 6)])
    _, _, g_dip = parse_track(gpx_of(dip), "gpx")
    check("a real dip is still counted when re-climbed",
          abs(g_dip - 100.0) < 1.0, f"{g_dip:.1f} m, expected ~100 m")

    # ---- "Recently earned" orders by date hiked, not date submitted ----
    # Seed the discriminating case: a completion submitted just now (newest
    # created_at) for a hike done months ago (old completed_on). Ordering by
    # created_at would put it first; ordering by completed_on must not.
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO completions (user_id,hike_id,completed_on,comment,created_at) "
                "VALUES (3,2,'2026-06-02','hiked in June, filed the form today',"
                "datetime('now'))")
    late_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.commit()
    con.close()

    with app.app_context(), app.test_request_context():
        from app.models import list_recent_completions
        recent = list_recent_completions(12)
        dates = [r["completed_on"] for r in recent]

    check("recent list sorts by completed_on descending",
          dates == sorted(dates, reverse=True), str(dates))
    check("a late submission does NOT jump the queue",
          recent[0]["id"] != late_id,
          f"late row {late_id} landed first: {dates}")
    check("a recently-hiked backfill DOES lead",
          recent[0]["completed_on"] == max(d for d in dates if d), str(dates))

    # The seeded flagged completion (user 3, counts = 0, completed 2025-03-20)
    # must not appear under a heading that says "earned".
    check("flagged completions are excluded from Recently earned",
          all(r["counts"] == 1 for r in recent),
          str([(r["user_name"], r["counts"]) for r in recent]))
    check("the flagged completion is genuinely in the DB to be excluded",
          sqlite3.connect(db_path).execute(
              "SELECT COUNT(*) FROM completions WHERE counts = 0").fetchone()[0] == 1)

    con = sqlite3.connect(db_path)
    con.execute("DELETE FROM completions WHERE id = ?", (late_id,))
    con.commit()
    con.close()

    # ---- Completion edit + delete, the write paths that touch thumb files ----
    con = sqlite3.connect(db_path)
    cid, = con.execute("SELECT id FROM completions ORDER BY id DESC LIMIT 1").fetchone()
    photo_id, old_thumb = con.execute(
        "SELECT id, thumb_filename FROM completion_photos WHERE completion_id = ?",
        (cid,)).fetchone()
    con.close()

    replacement = io.BytesIO()
    Image.new("RGB", (3000, 2000), (120, 30, 30)).save(replacement, "JPEG")
    replacement.seek(0)
    r = c.post(f"/completions/{cid}/edit",
               data={"completed_on": "2026-08-21", "comment": "edited",
                     "remove_photos": str(photo_id),
                     "photos": (replacement, "replacement.jpg")},
               content_type="multipart/form-data", follow_redirects=True)
    check("completion edit succeeds", "Completion updated" in r.get_data(as_text=True))
    check("removed photo's thumbnail deleted from disk",
          not (thumbs / old_thumb).exists() if old_thumb else True)
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT filename, thumb_filename FROM completion_photos "
                       "WHERE completion_id = ?", (cid,)).fetchall()
    con.close()
    check("edit left exactly the replacement photo", len(rows) == 1, str(rows))
    check("replacement photo got a thumbnail", rows and rows[0][1] is not None)
    if rows and rows[0][1]:
        check("replacement thumbnail on disk", (thumbs / rows[0][1]).exists())
    kept = rows[0] if rows else None

    r = c.post(f"/completions/{cid}/delete", follow_redirects=True)
    check("completion delete succeeds", "Completion deleted" in r.get_data(as_text=True))
    if kept:
        check("deleted completion's photo removed from disk",
              not (uploads / "photos" / kept[0]).exists())
        check("deleted completion's thumbnail removed from disk",
              not (thumbs / kept[1]).exists())

    # ---- Fresh-install path: new schema from scratch, migrations are no-ops ----
    print("\nSTEP 5 — fresh install")
    fresh_db = tmp / "fresh.db"
    r = subprocess.run([sys.executable, "tools/init_db.py", "--db", str(fresh_db)],
                       cwd=ROOT, capture_output=True, text=True)
    check("init_db.py runs clean", r.returncode == 0, r.stderr[-300:])
    fcon = sqlite3.connect(fresh_db)
    fcols = [row[1] for row in fcon.execute("PRAGMA table_info(hikes)")]
    check("fresh schema has posted_on and no active_to",
          "posted_on" in fcols and "active_to" not in fcols, str(fcols))
    check("fresh schema has thumb_filename",
          "thumb_filename" in [row[1] for row in
                               fcon.execute("PRAGMA table_info(completion_photos)")])
    fidx = [row[0] for row in fcon.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='hikes'")]
    check("fresh schema indexes posted_on", "idx_hikes_posted_on" in fidx, str(fidx))
    fcon.close()
    r = subprocess.run([sys.executable, "tools/migrate_open_hikes.py",
                        "--db", str(fresh_db), "--no-backup"],
                       cwd=ROOT, capture_output=True, text=True)
    check("migration is a clean no-op on a fresh DB",
          r.returncode == 0 and "already" in r.stdout, r.stdout + r.stderr)

    # ---- Admin notifications ----
    # Placed last because it logs in and out repeatedly and registers throwaway
    # accounts; keeping it at the end means nothing above depends on that mess.
    print("\nSTEP 6 — admin notifications")
    import app.mail as mail_module

    sent = []
    real_send_email = mail_module.send_email

    def capture(to, subject, body):
        sent.append({"to": to, "subject": subject, "body": body})
        return True

    def explode(to, subject, body):
        raise RuntimeError("SMTP is on fire")

    c.post("/auth/logout")
    mail_module.send_email = capture

    # --- Registration fires one notification ---
    app.config["NOTIFY_EMAIL"] = "ops@example.net"
    sent.clear()
    r = c.post("/auth/register",
               data={"name": "Notify Test", "email": "notify1@example.com",
                     "password": "hunter2hunter2"}, follow_redirects=True)
    check("registration succeeds", r.status_code == 200)
    check("registration sends exactly one notification", len(sent) == 1, str(len(sent)))
    if sent:
        check("notification goes to NOTIFY_EMAIL", sent[0]["to"] == "ops@example.net")
        check("subject is prefixed for filtering",
              sent[0]["subject"].startswith("[Challenge] "), sent[0]["subject"])
        check("body names the new member and their email",
              "Notify Test" in sent[0]["body"] and "notify1@example.com" in sent[0]["body"])
        check("body links to the new profile",
              "https://" in sent[0]["body"] or "/users/" in sent[0]["body"],
              sent[0]["body"])

    # --- Completion fires one notification ---
    c.post("/auth/logout")
    c.post("/auth/login", data={"email": "newbie@example.com",
                                "password": "hunter2hunter2"})
    sent.clear()
    shot = io.BytesIO()
    Image.new("RGB", (1200, 900), (60, 110, 60)).save(shot, "JPEG")
    shot.seek(0)
    r = c.post("/hikes/horseshoe-mesa/complete",
               data={"completed_on": "2025-06-15", "comment": "notification check",
                     "photos": (shot, "proof.jpg")},
               content_type="multipart/form-data", follow_redirects=True)
    check("completion succeeds", "earned the duck" in r.get_data(as_text=True))
    check("completion sends exactly one notification", len(sent) == 1, str(len(sent)))
    if sent:
        check("completion notification names hiker and hike",
              "New Member" in sent[0]["subject"] and "Horseshoe Mesa" in sent[0]["subject"],
              sent[0]["subject"])
        check("completion notification reports what landed",
              "Photos: 1" in sent[0]["body"], sent[0]["body"])

    # --- Unconfigured means silent, not broken ---
    c.post("/auth/logout")
    app.config["NOTIFY_EMAIL"] = None
    sent.clear()
    r = c.post("/auth/register",
               data={"name": "Quiet Test", "email": "notify2@example.com",
                     "password": "hunter2hunter2"}, follow_redirects=True)
    check("registration still works with notifications unconfigured",
          r.status_code == 200)
    check("nothing is sent when NOTIFY_EMAIL is unset", len(sent) == 0, str(len(sent)))

    # --- THE one that matters: a broken mailer must not cost a signup ---
    c.post("/auth/logout")
    app.config["NOTIFY_EMAIL"] = "ops@example.net"
    mail_module.send_email = explode
    r = c.post("/auth/register",
               data={"name": "Resilient Test", "email": "notify3@example.com",
                     "password": "hunter2hunter2"}, follow_redirects=True)
    check("registration survives a mailer that raises", r.status_code == 200,
          f"got {r.status_code}")
    con = sqlite3.connect(db_path)
    check("the account was created despite the mail failure",
          con.execute("SELECT COUNT(*) FROM users WHERE email = ?",
                      ("notify3@example.com",)).fetchone()[0] == 1)
    con.close()

    mail_module.send_email = real_send_email

    print(f"\n{'='*60}\n{len(PASSES)} passed, {len(FAILS)} failed")
    if FAILS:
        for f in FAILS:
            print(f"  FAILED: {f}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

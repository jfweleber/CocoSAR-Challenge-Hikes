# ===============================================================================
# Script Name:  migrate_open_hikes.py
# Purpose:      One-time migration to the "always open" Challenge model.
#               Drops hikes.active_to entirely and renames hikes.active_from
#               to hikes.posted_on, then adds completion_photos.thumb_filename.
#
#               Background: hikes used to have an active window, and a hike
#               whose window had passed was closed to new completions. That
#               locked new members out of every past route, which works against
#               the whole point of the program — area familiarization doesn't
#               expire. Now a hike has a posted date and nothing else: once it
#               drops, it stays open forever.
#
#               Idempotent — safe to run more than once. Each step checks the
#               live schema first, so a half-applied migration (interrupted
#               partway, say) finishes cleanly on a second run.
# Author:       Jamie F. Weleber
# Created:      August 2026
#
# Usage:        From the project root:
#                   python tools/migrate_open_hikes.py
#                   python tools/migrate_open_hikes.py --db /path/to/other.db
#                   python tools/migrate_open_hikes.py --no-backup
# ===============================================================================

# --- Imports ---
import argparse                      # optional --db / --no-backup flags
import shutil                        # file copy for the pre-flight backup
import sqlite3                       # stdlib driver; no app import needed for raw DDL
import sys                           # stdout encoding fix + import path
from datetime import datetime        # timestamp for the backup filename
from pathlib import Path             # OS-agnostic project-root resolution

# Make the app package importable so we read the SAME DATABASE path the app
# uses, rather than hardcoding it (keeps local dev and the server in sync).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")   # Windows cp1252 can't render the ✓ below

from app.config import BaseConfig    # BaseConfig.DATABASE is the canonical DB path

# ALTER TABLE ... DROP COLUMN landed in SQLite 3.35 (March 2021); RENAME COLUMN
# in 3.25. Ubuntu 24.04 ships 3.45 and any current Python bundles something
# newer still, so this should never trip — but failing with a clear message
# beats failing with "near DROP: syntax error" on some older box.
MIN_SQLITE = (3, 35, 0)


# ===============================================================================
# STEP 1: Helpers
# ===============================================================================

def columns(con, table):
    """Column names for a table. PRAGMA table_info returns one row per column
    with the name at index 1 — the standard way to introspect SQLite schema
    without parsing the CREATE statement text."""
    return [row[1] for row in con.execute(f"PRAGMA table_info({table})")]


def table_exists(con, name):
    """True if the named table is present. sqlite_master is SQLite's own
    catalog table; querying it is cheaper and more reliable than catching
    an OperationalError from a speculative SELECT."""
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def backup(db_path):
    """Copy the database next to itself with a timestamp suffix.

    Worth doing unconditionally here because this migration is the only one
    in the project that DESTROYS data: every hike's active_to value is gone
    the moment the column drops, and there is no way to reconstruct it from
    what remains. copy2 preserves mtime so the backup's timestamp reflects
    the database, not the moment of copying.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = db_path.with_name(f"{db_path.name}.{stamp}.bak")
    shutil.copy2(db_path, dest)
    return dest


# ===============================================================================
# STEP 2: Migration steps
# ===============================================================================

def migrate_hikes(con):
    """Drop active_to, rename active_from -> posted_on, fix the indexes."""
    cols = columns(con, "hikes")

    if "active_to" in cols:
        # SQLite refuses to drop a column that an index references, and the
        # original schema indexed active_to. The index has to go first —
        # this is the ordering constraint that makes the migration a script
        # rather than a one-liner.
        con.execute("DROP INDEX IF EXISTS idx_hikes_active_to")
        con.execute("ALTER TABLE hikes DROP COLUMN active_to")
        print("  ✓ dropped hikes.active_to (and its index) — hikes no longer close.")
    else:
        print("  · hikes.active_to already gone — skipping.")

    if "posted_on" in cols:
        print("  · hikes.posted_on already present — skipping rename.")
    elif "active_from" in cols:
        # RENAME COLUMN rewrites the column name in place and carries the
        # existing index along with it. We still drop and recreate the index
        # afterward so its NAME matches the new column — a stale
        # idx_hikes_active_from pointing at posted_on would work fine but
        # would confuse anyone reading .schema six months from now.
        con.execute("ALTER TABLE hikes RENAME COLUMN active_from TO posted_on")
        con.execute("DROP INDEX IF EXISTS idx_hikes_active_from")
        con.execute("CREATE INDEX IF NOT EXISTS idx_hikes_posted_on ON hikes(posted_on)")
        print("  ✓ renamed hikes.active_from -> hikes.posted_on (index rebuilt).")
    else:
        # Neither column exists — not a schema this script knows how to move.
        raise SystemExit(
            "  ! hikes table has neither active_from nor posted_on. "
            "Refusing to guess; check the database before continuing."
        )


def migrate_photo_thumbs(con):
    """Add completion_photos.thumb_filename for the thumbnail work."""
    if "thumb_filename" in columns(con, "completion_photos"):
        print("  · completion_photos.thumb_filename already present — skipping.")
        return

    # Nullable with no default, so every existing row reads NULL. That's the
    # correct starting state: those photos genuinely have no thumbnail yet.
    # Templates fall back to the full-size file on NULL, so the site is
    # correct (just heavy) until tools/migrate_thumbnails.py generates them.
    con.execute("ALTER TABLE completion_photos ADD COLUMN thumb_filename TEXT")
    print("  ✓ added completion_photos.thumb_filename "
          "(existing photos read NULL until the backfill runs).")


# ===============================================================================
# STEP 3: Entry point
# ===============================================================================

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=BaseConfig.DATABASE,
                        help="path to the SQLite DB file")
    parser.add_argument("--no-backup", action="store_true",
                        help="skip the pre-flight .bak copy (not recommended)")
    args = parser.parse_args()

    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise SystemExit(
            f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ required for "
            f"ALTER TABLE ... DROP COLUMN; this Python has "
            f"{sqlite3.sqlite_version}."
        )

    db_path = Path(args.db)
    print(f"Checking {db_path} ...")
    if not db_path.exists():
        print("  no database file — nothing to migrate. "
              "Run tools/init_db.py for a fresh install.")
        return

    con = sqlite3.connect(str(db_path))
    if not table_exists(con, "hikes"):
        print("  no hikes table yet — fresh DB, run init_db.py instead. Nothing to do.")
        con.close()
        return

    if not args.no_backup:
        dest = backup(db_path)
        print(f"  ✓ backup written to {dest.name}")

    try:
        migrate_hikes(con)
        migrate_photo_thumbs(con)
        con.commit()
    finally:
        con.close()

    print("Done. Next: python tools/migrate_thumbnails.py")


# Entry-point guard: run only on direct execution, not on import.
if __name__ == "__main__":
    main()

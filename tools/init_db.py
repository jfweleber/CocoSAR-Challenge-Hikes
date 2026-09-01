# ===============================================================================
# Script Name:  init_db.py
# Purpose:      Create the SQLite schema for the Challenge Hike site.
#               Idempotent without --force; with --force, drops existing
#               tables first so a fresh schema can be applied.
# Author:       Jamie F. Weleber
# Created:      May 18, 2026
# ===============================================================================
"""Create the SQLite schema for the Challenge Hike site.

Usage (from project root):
    python tools/init_db.py            # create tables if missing
    python tools/init_db.py --force    # drop all tables first, then recreate

Idempotent without --force (uses CREATE TABLE IF NOT EXISTS).
"""

import argparse              # Standard CLI argument parsing
import sqlite3               # Stdlib SQLite driver — no third-party dep needed for schema work
import sys                   # sys.path manipulation so we can import from app/
from pathlib import Path     # Cross-platform path handling (Windows local + Linux server)

# ===============================================================================
# STEP 1: Make the app package importable
# ===============================================================================
# This script lives under tools/, but it reuses the same SCHEMA_SQL string the
# Flask app uses at runtime (single source of truth — change the schema in
# app/models.py and this script picks it up automatically). Adding the project
# root to sys.path lets the `from app.models ...` import below succeed without
# having to pip-install the app.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DevConfig     # Default DB path for local dev
from app.models import SCHEMA_SQL    # Authoritative schema, shared with the running app

# ===============================================================================
# STEP 2: Define drop order
# ===============================================================================
# Foreign keys mean child tables must drop before their parents. SQLite's
# foreign_keys pragma enforces this when ON — listing the order here explicitly
# is safer than relying on iteration order of a set or the textual order in
# the schema string.
TABLES_IN_DROP_ORDER = (
    "password_resets",
    "completion_tracks",
    "completion_photos",
    "completions",
    "hikes",
    "users",
)


def main():
    """Parse CLI args and apply the schema to the configured database file."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="drop existing tables before recreating")
    parser.add_argument("--db", default=DevConfig.DATABASE,
                        help="path to SQLite DB file")
    args = parser.parse_args()

    # ===========================================================================
    # STEP 3: Ensure the DB file's directory exists
    # ===========================================================================
    # SQLite happily creates the .db file on first connect, but it won't create
    # the parent directory. mkdir(parents=True, exist_ok=True) is idempotent —
    # safe to call whether the dir exists or not.
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # ===========================================================================
    # STEP 4: Apply the schema
    # ===========================================================================
    conn = sqlite3.connect(str(db_path))
    try:
        # foreign_keys is OFF by default in SQLite per connection. Turning it
        # ON here ensures CASCADE clauses in the schema actually take effect
        # if --force is used to drop in dependency order.
        conn.execute("PRAGMA foreign_keys = ON")
        if args.force:
            for table in TABLES_IN_DROP_ORDER:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        # executescript runs multiple statements separated by semicolons —
        # the right call for a multi-CREATE schema like ours.
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()

    print(f"Database initialized at {db_path}")


# Standard "only run main() when invoked as a script, not when imported" guard.
# Lets this file be imported (e.g. by future tests) without side effects.
if __name__ == "__main__":
    main()

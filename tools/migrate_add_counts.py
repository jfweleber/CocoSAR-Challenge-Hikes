# ===============================================================================
# Script Name:  migrate_add_counts.py
# Purpose:      One-time migration: add the completions.counts column to an
#               existing database. Fresh installs already get it from SCHEMA_SQL;
#               this is only for databases created before the column existed.
#               Idempotent — safe to run more than once.
# Author:       Jamie F. Weleber
# Created:      June 2026
#
# Usage:        From the project root:  python tools/migrate_add_counts.py
# ===============================================================================

# --- Imports ---
import sqlite3                       # talk to the SQLite file directly (no app needed)
import sys                           # stdout encoding fix + import path
from pathlib import Path             # OS-agnostic project-root resolution

# Make the app package importable so we read the SAME DATABASE path the app uses,
# rather than hardcoding it (keeps local dev and the server in sync).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")   # Windows cp1252 can't render the ✓ below

from app.config import BaseConfig    # BaseConfig.DATABASE is the canonical DB path

def main():
    db_path = BaseConfig.DATABASE
    print(f"Checking {db_path} ...")

    con = sqlite3.connect(db_path)

    # A brand-new (or absent) database has no completions table yet — there's
    # nothing to migrate; init_db.py will create it with the column from SCHEMA_SQL.
    table_exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='completions'"
    ).fetchone()
    if not table_exists:
        print("  no completions table yet — fresh DB, run init_db.py instead. Nothing to do.")
        con.close()
        return

    # PRAGMA table_info returns one row per column; index 1 is the column name.
    columns = [row[1] for row in con.execute("PRAGMA table_info(completions)")]

    if "counts" in columns:
        print("  counts column already present — nothing to do.")
    else:
        # SQLite backfills the NOT NULL DEFAULT for every existing row, so all
        # current completions become counts=1 (they keep counting). New flag.
        con.execute(
            "ALTER TABLE completions ADD COLUMN counts INTEGER NOT NULL DEFAULT 1"
        )
        con.commit()
        print("  ✓ added completions.counts (existing completions default to 1 = counts).")

    con.close()
    print("Done.")

# Entry-point guard: run only on direct execution, not on import.
if __name__ == "__main__":
    main()

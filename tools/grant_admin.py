# ===============================================================================
# Script Name:  grant_admin.py
# Purpose:      Promote an existing user to admin by email. Needed once after
#               the first user registers, since the registration form has no
#               UI for granting admin (intentional — see Out of Scope in
#               CLAUDE.md). Lookup is case-insensitive.
# Author:       Jamie F. Weleber
# Created:      May 18, 2026
# ===============================================================================
"""Promote an existing user to admin by email.

Usage (from project root):
    python tools/grant_admin.py user@example.com

The lookup is case-insensitive (COLLATE NOCASE) so capitalization in the
email doesn't matter. Exits non-zero if no matching user is found.
"""

import sqlite3              # Stdlib SQLite driver — no app context needed for a one-shot UPDATE
import sys                  # CLI args, exit codes, sys.path tweak
from pathlib import Path    # Cross-platform path handling

# Same trick as init_db.py: make the app package importable so we share the
# canonical DB path from DevConfig instead of hardcoding it here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import DevConfig


def main():
    """Set users.is_admin = 1 for the email passed as argv[1]."""
    if len(sys.argv) != 2:
        print("Usage: python tools/grant_admin.py <email>")
        sys.exit(1)

    # Normalize to lowercase to match how the auth code stores emails on
    # registration. COLLATE NOCASE on the WHERE clause makes the match
    # case-insensitive even if old rows weren't normalized.
    email = sys.argv[1].strip().lower()

    conn = sqlite3.connect(DevConfig.DATABASE)
    try:
        cur = conn.execute(
            "UPDATE users SET is_admin = 1 WHERE email = ? COLLATE NOCASE",
            (email,),
        )
        conn.commit()
    finally:
        conn.close()

    # rowcount tells us whether any row actually matched. SQLite reports 0
    # for "WHERE matched nothing" — distinct from a syntax error, which
    # would have raised before reaching this line.
    if cur.rowcount == 0:
        print(f"No user found with email {email}")
        sys.exit(1)

    print(f"User {email} is now admin.")


if __name__ == "__main__":
    main()

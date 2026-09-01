# ===============================================================================
# Module:   app/timeutils.py
# Purpose:  Arizona-local time helpers. Every "is this hike upcoming / active /
#           closed today?" decision in the app must be made against the date in
#           Arizona, NOT the server's clock. Production runs on a UTC box, so the
#           stdlib date.today() there rolls over at 5 PM Arizona — which would
#           open and close Challenge windows seven hours early. These helpers
#           are the single source of "now" for that logic.
# Author:   Jamie F. Weleber
# Created:  June 1, 2026
# ===============================================================================
"""Arizona-local date/time helpers (MST, fixed UTC-7, no DST)."""

from datetime import datetime, timedelta, timezone     # tz-aware UTC now + a fixed offset shift


# Coconino County observes Mountain Standard Time all year. Arizona is one of
# the few US areas that does NOT observe daylight saving, so a fixed -7h offset
# is exact year-round — there is no spring/fall edge case to get wrong, and we
# avoid taking a dependency on the IANA tz database (zoneinfo/tzdata) just to
# compute a date the team already knows never shifts.
ARIZONA_OFFSET = timedelta(hours=-7)


def now_az():
    """Current wall-clock datetime in Arizona, as a naive datetime.

    Computed from tz-aware UTC so it's correct regardless of the host's own
    timezone setting (dev on Windows-local, prod on a UTC server)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + ARIZONA_OFFSET


def today_az():
    """Today's date in Arizona — the date the whole app should treat as "today".

    Use this anywhere a hike's active window is evaluated (home page current
    hike, catalog/detail state pills, completion-window validation). Swapping
    date.today() for this is what keeps a hike open through the end of its last
    day in Arizona instead of closing it at 5 PM local."""
    return now_az().date()

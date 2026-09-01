# CocoSAR Challenge Hikes

A participation tracker for the **Coconino County Sheriff's Search and Rescue
Mountain Rescue Unit**. A team member posts a Challenge Hike route roughly
quarterly; anyone who completes it uploads proof and earns a rubber duck. The
site keeps the catalog, the maps, and the tally.

Runs in production at `challenge.weleber.net`.

## What it does

- Features the newest posted route on a Leaflet map with an elevation profile
  derived from the route's own GPX/KML elevation data
- Keeps a catalog of every Challenge ever posted. **Hikes never close** — a
  hike carries a posted date and nothing else, and accepts completions forever
  after that. Joining the team late shouldn't lock you out of the back catalog
- Accepts completion proof: photos and/or recorded GPX/KML tracks, parsed
  server-side and stored as GeoJSON so the browser never parses XML
- Shows a team-wide leaderboard of duck counts, per-hike finisher lists,
  per-member profiles, and a public page per completion
- Emails the operator when someone registers or logs a completion

## Stack

Python 3.12, Flask, SQLite via the stdlib `sqlite3` module, Gunicorn behind
Nginx. Server-rendered Jinja templates with vanilla JavaScript in external
files — no SPA framework and no build step. Leaflet for maps, `gpxpy` and
`lxml` for track parsing, Pillow for photo processing.

Authentication is Flask-Login with werkzeug's PBKDF2 password hashing. There is
no custom cryptography anywhere in this codebase, which is deliberate.

## Running locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

python tools/init_db.py         # creates the schema; safe to re-run
flask --app app run             # http://127.0.0.1:5000
```

No configuration is required for local development. With `SMTP_HOST` unset the
mail module logs messages to the console instead of sending them, so the
password-reset flow can be exercised end to end without real SMTP credentials.

`tools/` also holds `grant_admin.py` and the migration scripts.

## Configuration

Production reads its settings from the environment. See
[`challenge.env.example`](challenge.env.example) for the full list with notes —
the short version is a `SECRET_KEY`, five `SMTP_*`/`MAIL_FROM` values for
outbound mail, and an optional `NOTIFY_EMAIL` for operator notifications.

One trap worth repeating from that file: `SMTP_USER` must be the exact address
the SMTP token was issued for, and `MAIL_FROM` must match it. A mismatch fails
authentication, and because the password-reset route deliberately shows the
same generic response whether or not mail succeeded, it fails *silently*.

## Deployment

Gunicorn on a Unix socket, managed by systemd, reverse-proxied by Nginx which
terminates TLS. `ProxyFix` is wired up in the app factory so `X-Forwarded-Proto`
is honored — without it, password-reset emails would contain `http://` links.

The systemd unit needs `PYTHONUNBUFFERED=1` together with gunicorn's
`--capture-output --enable-stdio-inheritance`, or application logging is
swallowed and never reaches the journal.

## Data and privacy

The database and the `uploads/` directory are **not in this repository and never
will be**. They hold real names, email addresses, password hashes, personal
photographs, and recorded GPS tracks belonging to identifiable members of an
active search-and-rescue team. `tools/init_db.py` builds an empty schema; the
upload directories are created at startup.

## License

Licensed under the **GNU Affero General Public License v3.0**. See
[LICENSE](LICENSE).

The AGPL is a deliberate choice for a web application: section 13 means that if
you run a modified version of this software as a network service, you must offer
its source to the people using it. Fork it, adapt it for your own SAR team,
change whatever you like — just keep it open for the people it serves.

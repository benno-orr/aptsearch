# aptsearch

Apartment listing tracker for a 1BR in Cambridge/Somerville. Scrapes Craigslist,
Apartments.com, Zillow, Rent.com, HotPads, and Facebook Marketplace; scores
listings by commute to Kendall/Broad; serves a web UI with status + notes.

- `track.py` — scraper + CLI (the engine)
- `server.py` — local web UI (status buttons, notes)
- `search.py`, `auth.py` — helpers

## Shared database (Turso)

The listing data lives in a **shared remote SQLite database** (Turso / libSQL) so
that anyone with a clone of this repo can read and write the same listings — when
one person changes a status, adds a note, or runs a scrape, everyone sees it.

If no credentials are configured, the app falls back to a local `listings.db`
file, so you can still work offline.

### One-time setup (per person)

1. **Use the Python 3.12 venv.** The libSQL driver needs Python ≥ 3.10, and the
   project's only other dependency is `playwright` (for scraping). Create the venv
   and install deps:

   ```bash
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/playwright install chromium   # only needed for the fetch-* scrapers
   ```

   Run everything through `.venv/bin/python` (e.g. `.venv/bin/python track.py ...`),
   or activate it with `source .venv/bin/activate`.

2. **Add the shared DB credentials.** Copy the template and paste in the URL +
   token (ask whoever set up the Turso DB, or get them from
   <https://turso.tech>). The file is gitignored.

   ```bash
   cp .turso_key.example .turso_key
   # then edit .turso_key
   ```

3. **Verify the connection:**

   ```bash
   .venv/bin/python track.py db-status
   ```

   You should see `Mode: SHARED (Turso)` and the listing count.

### Creating the shared DB (one person, once)

1. Sign up at <https://turso.tech> and create a database (free tier is plenty).
2. Grab its database URL (`libsql://…`) and create an auth token.
3. Put both in your `.turso_key`.
4. Push the existing local data up to it:

   ```bash
   .venv/bin/python track.py db-push        # uploads listings.db rows to Turso
   ```

5. Share the URL + token with collaborators (out-of-band, e.g. a password
   manager — **not** via git).

### Database commands

| Command | What it does |
|---|---|
| `track.py db-status` | Show whether you're on the shared (Turso) or local DB, and the row count |
| `track.py db-push`   | Upload local `listings.db` rows into the shared DB (`--wipe` to replace remote first) |
| `track.py db-pull`   | Download the shared DB into a local `listings.db` (backs up any existing one; `--wipe` to replace local first) |

## Notes

- Secrets (`.turso_key`, `.google_key`) and per-user artifacts (`listings.db`,
  browser profiles, `.venv/`) are gitignored — only code is committed.
- Without `.turso_key`, the app silently uses the local `listings.db`.

#!/usr/bin/env python3
"""
Apartment listing tracker.

Usage:
  python3 track.py add <url> [--title "..."] [--price 2400] [--location "..."] [--notes "..."]
  python3 track.py list [--status new|viewed|interested|passed|applied]
  python3 track.py show <id>
  python3 track.py view <id>
  python3 track.py interest <id>
  python3 track.py pass <id>
  python3 track.py apply <id>
  python3 track.py note <id> <text>
  python3 track.py edit <id> [--title "..."] [--price ...] [--location "..."]
  python3 track.py delete <id>
  python3 track.py fetch-cl         # scrape Craigslist (uses Playwright if installed)
  python3 track.py fetch-fb         # scrape Facebook Marketplace (requires Playwright + login)
  python3 track.py fetch-apts       # scrape Apartments.com (requires Playwright)
  python3 track.py update           # fetch-cl + auto-add new to DB
  python3 track.py html             # export all listings to listings.html
"""

import sqlite3
import argparse
import sys
import os
import re
import hashlib
import urllib.request
import urllib.parse
import gzip
import asyncio
import json as _json
from html.parser import HTMLParser
from datetime import datetime

SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
DB_PATH         = os.path.join(SCRIPT_DIR, "listings.db")
TURSO_KEY_PATH  = os.path.join(SCRIPT_DIR, ".turso_key")        # shared remote DB creds (gitignored)
HTML_OUT        = os.path.join(SCRIPT_DIR, "listings.html")
FB_COOKIES_PATH = os.path.join(SCRIPT_DIR, "fb_cookies.json")   # legacy (pre-profile)
FB_PROFILE_DIR  = os.path.join(SCRIPT_DIR, ".fb_profile")        # persistent Chrome profile

STATUS_ORDER  = ["new", "viewed", "interested", "applied", "passed", "gotaway"]
STATUS_ICONS  = {"new": "[ ]", "viewed": "[~]", "interested": "[★]", "applied": "[✓]", "passed": "[✗]"}
STATUS_COLORS = {
    "new":        "\033[0m",
    "viewed":     "\033[33m",
    "interested": "\033[32m",
    "applied":    "\033[34m",
    "passed":     "\033[90m",
}
RESET = "\033[0m"

NEIGHBORHOODS = [
    {"name": "East Cambridge",  "transit": "Walk/bike to Kendall (~15 min)",         "avg": "$2,600–$3,200", "score": 5},
    {"name": "Central Square",  "transit": "Red Line → Kendall: 1 stop (~4 min)",     "avg": "$2,400–$2,900", "score": 5},
    {"name": "Inman Square",    "transit": "Bus/bike to Kendall ~20 min",             "avg": "$2,200–$2,700", "score": 4},
    {"name": "Union Square",    "transit": "Green Line Ext → Kendall ~25 min",        "avg": "$2,100–$2,600", "score": 4},
    {"name": "Cambridgeport",   "transit": "Bike/bus to Kendall ~15 min",             "avg": "$2,200–$2,700", "score": 4},
    {"name": "Porter Square",   "transit": "Red Line → Kendall: 2 stops (~9 min)",    "avg": "$2,200–$2,800", "score": 3},
    {"name": "Davis Square",    "transit": "Red Line → Kendall: 3 stops (~14 min)",   "avg": "$2,100–$2,700", "score": 3},
    {"name": "Harvard Square",  "transit": "Red Line → Kendall: 2 stops (~7 min)",    "avg": "$2,400–$3,000+","score": 3},
    {"name": "Winter Hill",     "transit": "Bus/walk to Davis Red Line",              "avg": "$2,000–$2,500", "score": 2},
    {"name": "Magoun Square",   "transit": "Green Line Ext, transfer needed",         "avg": "$1,900–$2,400", "score": 2},
    {"name": "Ball Square",     "transit": "Green Line Ext, transfer needed",         "avg": "$2,000–$2,500", "score": 2},
]
_NEIGHBORHOOD_MAP = {n["name"].lower(): n["score"] for n in NEIGHBORHOODS}

# Boston-specific house-unit keywords (triple-deckers, townhouses, converted houses, etc.)
_HOUSE_RE = re.compile(
    r'\b(house|floor\s+of|triple.?decker|3.?decker|2.?decker|[23].?family|multi.?family|'
    r'townhouse|town.?house|duplex|in.?law|converted|single.?family|colonial|victorian|'
    r'cape\s+cod|craftsman|bungalow|ranch|condo|row.?house|brownstone|carriage|casa)\b',
    re.IGNORECASE
)

def house_score(text):
    """Return 1 if title/location suggests a unit within a house rather than a complex."""
    return 1 if text and _HOUSE_RE.search(text) else 0


def row_is_house(r):
    """House flag for a DB row: stored is_house bit, else keyword match."""
    try:
        if r["is_house"]:
            return 1
    except (KeyError, IndexError):
        pass
    return house_score((r["title"] or "") + " " + (r["location"] or ""))


# HARD CONSTRAINT: no rooms in shared units. \b keeps "bedroom" from matching "room".
_SHARED_RE = re.compile(
    r'\b(room\s+(?:for\s+rent|in|available)|roommate|housemate|'
    r'shared\s+(?:apartment|apt|house|housing|unit|living)|'
    r'private\s+(?:bed)?room\s+in|co.?living|rooming\s+house|'
    r'sublet.{0,12}room|room\s+sublet|furnished\s+room|spare\s+room)\b',
    re.IGNORECASE
)

def is_shared(text):
    """True if the listing is a room in a shared unit (excluded outright)."""
    return bool(text and _SHARED_RE.search(text))


# SOFT PREFERENCE: in-unit laundry. Matches "in-unit laundry/washer", "W/D in
# unit", "washer & dryer", "private laundry" — NOT "laundry facilities/in building".
_LAUNDRY_RE = re.compile(
    r'\b(in.?unit\s+(?:laundry|washer|w/?d)|laundry\s+in.?unit|'
    r'w/?d\s+in.?unit|washer.{0,8}dryer|private\s+laundry)\b',
    re.IGNORECASE
)

def laundry_score(text):
    return 1 if text and _LAUNDRY_RE.search(text) else 0


def row_has_laundry(r):
    """Laundry flag for a DB row: stored bit, else keyword match on title."""
    try:
        if r["has_laundry"]:
            return 1
    except (KeyError, IndexError):
        pass
    return laundry_score(r["title"] or "")


# MOVE-IN TARGET: Sept 1. Detects "9/1", "Sept 1", "September" in listing text.
_SEPT_RE = re.compile(r'\b(9[/.]1\b|sept?\.?\s*(?:1st|1|first)?\b|september)\b', re.IGNORECASE)

def sept_score(text):
    return 1 if text and _SEPT_RE.search(text) else 0


_MONTH_NUM = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
              'jul': 7, 'aug': 8, 'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12}


def epoch_ms_to_ymd(ms):
    """Epoch milliseconds → 'YYYY-MM-DD', or None on bad input."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def _fmt_md(mo, day):
    """(month_int, day_int_or_None) → 'Sep 1' / 'Sep', or '' if out of range."""
    if not (1 <= mo <= 12):
        return ""
    if day and 1 <= day <= 31:
        return f"{_MONTHS[mo]} {day}"
    return _MONTHS[mo]


def parse_move_in(value):
    """Normalize any availability/move-in value to a short display string
    ('Sep 1', 'Aug 15', 'Now') or '' when unknown/unparseable.

    Handles: epoch ms, ISO dates ('2026-09-01T..'), 'M/D', month names
    ('Aug 1', 'September 1st', 'Sept'), and 'available now / asap / immediately'."""
    if value is None:
        return ""
    # epoch milliseconds (e.g. HotPads timestamps)
    if isinstance(value, (int, float)):
        ymd = epoch_ms_to_ymd(value)
        if ymd:
            return _fmt_md(int(ymd[5:7]), int(ymd[8:10]))
        return ""
    s = str(value).strip()
    if not s:
        return ""
    low = s.lower()
    if any(w in low for w in ("now", "immediat", "asap", "today", "ready to", "available now")):
        return "Now"
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)            # ISO date / datetime
    if m:
        return _fmt_md(int(m.group(2)), int(m.group(3)))
    m = re.match(r'(\d{1,2})[/.](\d{1,2})', s)             # M/D or M/D/YY
    if m:
        return _fmt_md(int(m.group(1)), int(m.group(2)))
    m = re.search(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)[a-z]*\.?\s*(\d{0,2})', low)
    if m:                                                   # month name + optional day
        day = int(m.group(2)) if m.group(2) else None
        return _fmt_md(_MONTH_NUM[m.group(1)], day)
    return ""


def movein_iso(avail):
    """Stored move-in display ('Sep 1', 'Now', '') → ISO date for filtering, or ''.
    'Now'/blank → '' (no constraint). A bare month/day gets the soonest future
    year (this year, or next if already past)."""
    if not avail:
        return ""
    s = avail.strip().lower()
    if "now" in s:
        return ""
    m = re.match(r'([a-z]{3,4})\s*(\d{0,2})', s)
    mo = _MONTH_NUM.get(m.group(1)) if m else None
    if not mo:
        return ""
    day = int(m.group(2)) if (m and m.group(2)) else 1
    today = datetime.now().date()
    try:
        d = today.replace(year=today.year, month=mo, day=day)
    except ValueError:
        return ""
    if d < today:
        try:
            d = d.replace(year=today.year + 1)
        except ValueError:
            return ""
    return d.isoformat()


def row_available(r):
    try:
        return r["available"] or ""
    except (KeyError, IndexError):
        return ""

# Craigslist: /search/apa = apartments/housing for rent.
# No query param — a text query is treated as an exact phrase and kills results;
# we filter to Cambridge/Somerville client-side via _CAMBSOM_RE instead.
# housing_type 4,6,7,9 = duplex, house, in-law, townhouse.
SEARCH_URL = (
    "https://boston.craigslist.org/search/apa?"
    "min_price=2000&max_price=2800"
    "&min_bedrooms=1&max_bedrooms=1"
    "&housing_type=4&housing_type=6&housing_type=7&housing_type=9"
    "&availabilityMode=0"
)
# Broad CL search (all housing types) — the primary scrape target.
# availabilityMode=2 = "beyond 30 days": targets the Sept 1 move-in cycle.
# REVISIT in early August (switch to 1 or 0 as Sept 1 gets <30 days out).
SEARCH_URL_ALL = (
    "https://boston.craigslist.org/search/apa?"
    "min_price=2000&max_price=2800"
    "&min_bedrooms=1&max_bedrooms=1"
    "&availabilityMode=2"
)

# Cambridge/Somerville location filter (applied to title + location text)
_CAMBSOM_RE = re.compile(
    r'\b(cambridge|somerville|kendall|central\s*sq|inman|union\s*sq|porter|davis|'
    r'harvard|cambridgeport|winter\s*hill|magoun|ball\s*sq|teele|assembly|lechmere|'
    r'spring\s*hill|prospect\s*hill|east\s*som)\b',
    re.IGNORECASE
)

def in_camb_som(text):
    return bool(text and _CAMBSOM_RE.search(text))


# Nearby towns whose listings sometimes spam "cambridge" in the location field.
# The CL URL slug (/d/<city>-...) is geocoded and more trustworthy than freetext.
_CL_TOWN_BLACKLIST = {
    "woburn", "malden", "medford", "everett", "revere", "chelsea", "quincy",
    "waltham", "watertown", "arlington", "belmont", "newton", "brookline",
    "brighton", "allston", "dorchester", "roxbury", "lynn", "salem", "stoneham",
    "winchester", "burlington", "billerica", "lowell", "brockton", "framingham",
}

def cl_keep(r):
    """Keep a CL listing if it's genuinely Cambridge/Somerville."""
    m = re.search(r'/d/([a-z]+)', r.get("url", ""))
    slug_city = m.group(1) if m else ""
    if slug_city in ("cambridge", "somerville"):
        return True
    if slug_city in _CL_TOWN_BLACKLIST:
        return False
    return in_camb_som((r.get("title") or "") + " " + (r.get("location") or ""))

FB_SEARCH_URL = (
    "https://www.facebook.com/marketplace/boston/propertyrentals"
    "?minPrice=2000&maxPrice=2800&bedrooms=1"
)

# Apartments.com: filters MUST be in the URL path (query params are ignored).
# Houses/townhomes/condos first (house-unit preference), then general 1BR.
# Third field: 1 = this page lists house-type units (tag results as house).
APTS_URLS = [
    ("Cambridge houses+th",  "https://www.apartments.com/houses-townhomes-condos/cambridge-ma/1-bedrooms-2000-to-2800/", 1),
    ("Somerville houses+th", "https://www.apartments.com/houses-townhomes-condos/somerville-ma/1-bedrooms-2000-to-2800/", 1),
    ("Cambridge 1BR all",    "https://www.apartments.com/cambridge-ma/1-bedrooms-2000-to-2800/", 0),
    ("Somerville 1BR all",   "https://www.apartments.com/somerville-ma/1-bedrooms-2000-to-2800/", 0),
]

_SEARCH_LINKS = [
    ("Craigslist — Cambridge/Somerville (house types)",
     SEARCH_URL),
    ("Craigslist — all types",
     SEARCH_URL_ALL),
    ("Facebook Marketplace — Boston Rentals",
     "https://www.facebook.com/marketplace/boston/propertyrentals?minPrice=2000&maxPrice=2800&bedrooms=1"),
    ("Apartments.com — Cambridge houses/townhomes/condos 1BR",
     "https://www.apartments.com/houses-townhomes-condos/cambridge-ma/1-bedrooms-2000-to-2800/"),
    ("Apartments.com — Somerville houses/townhomes/condos 1BR",
     "https://www.apartments.com/houses-townhomes-condos/somerville-ma/1-bedrooms-2000-to-2800/"),
    ("Apartments.com — Cambridge 1BR",
     "https://www.apartments.com/cambridge-ma/1-bedrooms-2000-to-2800/"),
    ("Apartments.com — Somerville 1BR",
     "https://www.apartments.com/somerville-ma/1-bedrooms-2000-to-2800/"),
    ("Zillow — Cambridge 1BR $2000-2800",
     "https://www.zillow.com/cambridge-ma/rentals/1-_beds/2000-2800_mp/"),
    ("Zillow — Somerville 1BR $2000-2800",
     "https://www.zillow.com/somerville-ma/rentals/1-_beds/2000-2800_mp/"),
    ("Rent.com — Cambridge 1BR",
     "https://www.rent.com/massachusetts/cambridge-apartments?bedrooms=1"),
    ("Rent.com — Somerville 1BR",
     "https://www.rent.com/massachusetts/somerville-apartments?bedrooms=1"),
    ("Redfin — Cambridge",
     "https://www.redfin.com/city/2833/MA/Cambridge/1-bedroom-apartments-for-rent"),
    ("Redfin — Somerville",
     "https://www.redfin.com/city/16064/MA/Somerville/apartments-for-rent"),
    ("Boston Pads — Cambridge",
     "https://bostonpads.com/cambridge-ma-apartments/"),
    ("Boston Pads — Somerville",
     "https://bostonpads.com/somerville-ma-apartments/"),
    ("HotPads — Cambridge 1BR $2000-2800",
     "https://hotpads.com/cambridge-ma/apartments-for-rent?beds=1-1&price=2000-2800"),
    ("HotPads — Somerville 1BR $2000-2800",
     "https://hotpads.com/somerville-ma/apartments-for-rent?beds=1-1&price=2000-2800"),
]

# ── HTML template ──────────────────────────────────────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Apartments — Cambridge / Somerville</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet-polylineoffset@1.1.1/leaflet.polylineoffset.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,-apple-system,sans-serif;background:#f0f2f5;color:#1a1a2e;padding:24px}}
.page{{max-width:1480px;margin:0 auto}}
h1{{font-size:1.6em;margin-bottom:4px}}
.subtitle{{color:#666;font-size:0.9em;margin-bottom:32px}}
.section{{margin-bottom:28px}}
.sec-header{{display:flex;align-items:center;gap:10px;margin-bottom:14px;border-bottom:2px solid #e5e7eb;padding-bottom:8px}}
.sec-title{{font-size:0.85em;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#555}}
.count{{background:#e5e7eb;color:#555;font-size:0.75em;padding:2px 8px;border-radius:10px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:14px}}
.media-row{{display:flex;flex-direction:column;gap:6px}}
.media-row .thumb-link{{display:block;width:100%;line-height:0}}
.media-row .thumb{{width:100%;height:210px;object-fit:cover;border-radius:0}}
.delisted-banner{{background:#7f1d1d;color:#fff;font-size:0.7em;font-weight:700;text-transform:uppercase;letter-spacing:.05em;padding:4px 12px;text-align:center}}
.card.delisted{{opacity:.55;filter:grayscale(.4)}}
.dup-banner{{background:#6b7280;color:#fff;font-size:0.7em;font-weight:700;text-transform:uppercase;letter-spacing:.05em;padding:4px 12px;text-align:center}}
.card.duplicate{{opacity:.7}}
.minimap-wrap{{position:relative;flex:1;min-width:0}}
.map-bubbles{{position:absolute;top:6px;right:6px;z-index:500;display:flex;flex-direction:column;gap:4px;align-items:flex-end;pointer-events:none}}
.map-bub{{background:rgba(255,255,255,.95);border-radius:9px;padding:1px 7px;font-size:11px;font-weight:700;line-height:18px;box-shadow:0 1px 2px rgba(0,0,0,.3);white-space:nowrap}}
.bub-walk{{color:#15803d;border:1px solid #16a34a}}
.bub-transit{{color:#6d28d9;border:1px solid #7c3aed}}
.bub-bike{{color:#c2410c;border:1px solid #ea580c}}
.media-split{{display:flex;gap:6px}}
.media-split > *{{flex:1;min-width:0}}
.media-row .minimap{{width:100%;height:180px;aspect-ratio:auto;margin:0;border-radius:0}}
.media-row .sv-link{{display:block;width:100%;line-height:0;position:relative}}
.media-row .streetview{{width:100%;height:180px;object-fit:cover;border-radius:0}}
.sv-tag{{position:absolute;left:8px;bottom:8px;background:rgba(0,0,0,.6);color:#fff;font-size:0.66em;font-weight:700;padding:2px 7px;border-radius:6px;line-height:1.4}}
.card{{position:relative;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);border-left:4px solid #e5e7eb}}
/* bottom "how much I like" row: ❌ reject + 🤔/😊/😍 */
.rating-row{{display:flex;justify-content:center;gap:10px;margin-top:12px;padding-top:10px;border-top:1px solid #f3f4f6}}
.rating-row .rate{{font-size:1.5em;line-height:1;width:50px;height:46px;border-radius:12px;border:2px solid transparent;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0;opacity:.85;transition:transform .08s}}
.rating-row .rate:hover{{opacity:1;transform:scale(1.08)}}
/* HSB(hue, 50%% sat, 100%% brightness): hue 0 / 60 / 90 / 120 → red → green */
.rating-row .rate-pass{{background:#ff8080}}
.rating-row .rate-hmm{{background:#ffff80}}
.rating-row .rate-ok{{background:#bfff80}}
.rating-row .rate-love{{background:#80ff80}}
.rating-row .rate.rated-on{{opacity:1;border-color:#111;transform:scale(1.12);box-shadow:0 2px 8px rgba(0,0,0,.3)}}
/* status buttons: a small emoji column to the right of commute */
.status-col{{display:flex;flex-direction:column;gap:6px}}
.status-col .act{{font-size:1.15em;width:38px;height:32px;border:1px solid #d1d5db;border-radius:8px;background:#f9fafb;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0;text-align:center}}
.status-col .act:hover{{background:#eef2ff}}
.card.viewed .act-viewed,.card.applied .act-applied{{border-color:#111;background:#eef2ff}}
.price-line{{font-size:1.45em;font-weight:800;line-height:1.1;margin-bottom:3px;display:flex;align-items:baseline;gap:9px}}
.price-line a{{color:#111;text-decoration:none}}
.price-line a:hover{{text-decoration:underline}}
.price-line .permo{{font-size:0.6em;font-weight:600;color:#6b7280}}
.price-line .pl-meta{{font-size:0.5em;font-weight:600;color:#6b7280;display:flex;align-items:center;gap:5px}}
.price-line .pl-avail{{margin-left:auto;color:#6d28d9}}
.spec-line{{margin-bottom:5px;font-size:0.9em;font-weight:700;display:flex;gap:10px;flex-wrap:wrap}}
.spec-bba{{color:#1d4ed8}}
.spec-sqft{{color:#0891b2}}
.spec-avail{{color:#6d28d9}}
.units-avail{{margin:8px 0}}
.units-list{{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}}
.unit-chip{{background:#eef2ff;color:#3730a3;border:1px solid #c7d2fe;border-radius:8px;padding:2px 9px;font-size:0.82em;font-weight:600;text-decoration:none}}
.unit-chip:hover{{background:#e0e7ff;text-decoration:underline}}
.addr-line{{margin-bottom:8px;font-size:0.9em}}
.addr-line a{{color:#374151;text-decoration:none}}
.addr-line a:hover{{text-decoration:underline}}
.card.interested{{border-left-color:#22c55e}}
.card.applied{{border-left-color:#8b5cf6}}
.card.new{{border-left-color:#3b82f6}}
.card.viewed{{border-left-color:#f59e0b}}
.card.passed{{border-left-color:#d1d5db;opacity:.6}}
.card.is-house{{}}
.card-body{{padding:14px 16px}}
.thumb-link{{display:block;line-height:0}}
.thumb{{width:100%;height:180px;object-fit:cover;background:#eef0f3;display:block}}
.thumb-empty{{display:flex;align-items:center;justify-content:center;color:#bbb;font-size:0.8em;font-style:italic;height:120px}}
.card.no-img .thumb{{display:none}}
.new-today{{background:#dcfce7;color:#166534;font-size:0.7em;padding:1px 6px;border-radius:8px;font-weight:700}}
.card-title{{font-weight:600;margin-bottom:8px;line-height:1.35;font-size:0.95em}}
.card-title a{{color:#1d4ed8;text-decoration:none}}
.card-title a:hover{{text-decoration:underline}}
.meta{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:0.83em;color:#666;margin-bottom:8px}}
.price{{font-size:1.05em;font-weight:700;color:#111}}
.stars{{color:#f59e0b}}
.badge{{display:inline-block;padding:2px 8px;border-radius:12px;font-size:0.72em;font-weight:700;text-transform:uppercase}}
.badge-new{{background:#dbeafe;color:#1e40af}}
.badge-viewed{{background:#fef3c7;color:#92400e}}
.badge-interested{{background:#dcfce7;color:#166534}}
.badge-applied{{background:#ede9fe;color:#6d28d9}}
.badge-passed{{background:#f3f4f6;color:#6b7280}}
.house-badge{{display:inline-block;background:#fef3c7;color:#92400e;border:1px solid #fcd34d;font-size:0.72em;font-weight:700;padding:2px 7px;border-radius:10px;margin-left:4px;vertical-align:middle}}
.laundry-badge{{display:inline-block;background:#e0f2fe;color:#075985;border:1px solid #7dd3fc;font-size:0.72em;font-weight:700;padding:2px 7px;border-radius:10px;margin-left:4px;vertical-align:middle}}
.avail{{background:#f3e8ff;color:#6b21a8;font-size:0.78em;font-weight:600;padding:1px 7px;border-radius:8px}}
.commute{{background:#f0fdfa;color:#0f766e;font-size:0.8em;font-weight:600;padding:2px 8px;border-radius:8px}}
.ec-badge{{display:inline-block;background:#dc2626;color:#fff;font-size:0.7em;font-weight:800;letter-spacing:.04em;padding:2px 8px;border-radius:10px;margin-right:6px;vertical-align:middle}}
.card.east-cam{{}}
.actions{{display:flex;flex-direction:column;gap:5px}}
.act{{border:1px solid #d1d5db;background:#fff;border-radius:6px;padding:3px 9px;font-size:0.78em;cursor:pointer;color:#374151;text-align:left;white-space:nowrap}}
.act:hover{{background:#f3f4f6}}
.act-interested:hover{{background:#dcfce7;border-color:#22c55e}}
.act-passed:hover{{background:#fee2e2;border-color:#ef4444}}
.act-applied:hover{{background:#ede9fe;border-color:#8b5cf6}}
.act-viewed:hover{{background:#fef3c7;border-color:#f59e0b}}
.controls{{background:#fff;border-radius:10px;padding:12px 16px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:20px;display:flex;flex-wrap:wrap;gap:14px;align-items:center;font-size:0.88em}}
.controls label{{display:flex;align-items:center;gap:5px;cursor:pointer}}
.controls .spacer{{flex:1}}
.btn-refresh{{border:1px solid #3b82f6;background:#eff6ff;color:#1d4ed8;border-radius:6px;padding:5px 12px;font-size:0.92em;cursor:pointer}}
.btn-refresh:hover{{background:#dbeafe}}
.btn-refresh:disabled{{opacity:.5;cursor:wait}}
#refresh-log{{font-size:0.8em;color:#666;white-space:pre-wrap}}
.card-banner{{display:flex;justify-content:flex-start;align-items:center;gap:18px;padding:5px 12px;font-size:0.72em;font-weight:700;text-transform:uppercase;letter-spacing:.05em}}
.banner-date{{font-weight:600;opacity:.85}}
.banner-new{{color:#166534;background:#dcfce7;padding:0 7px;border-radius:6px;margin-left:auto}}
.addr-price{{color:#111;font-weight:800}}
.addr-loc{{color:#444}}
.addr-specs{{color:#1d4ed8;font-weight:600}}
.addr-avail{{color:#6b21a8;font-weight:700}}
.src{{display:inline-block;padding:1px 6px;border-radius:8px;font-size:0.68em;font-weight:600;margin-left:4px;vertical-align:middle;text-transform:uppercase}}
.src-craigslist{{background:#fff3cd;color:#856404}}
.src-facebook{{background:#dbeafe;color:#1e40af}}
.src-apartments{{background:#dcfce7;color:#166534}}
.src-zillow{{background:#ede9fe;color:#5b21b6}}
.src-rent{{background:#fee2e2;color:#b91c1c}}
.src-hotpads{{background:#fef3c7;color:#b45309}}
.notes{{margin-top:10px;font-size:0.82em;color:#555;background:#f9fafb;border-radius:6px;padding:8px 10px;white-space:pre-wrap;border-left:3px solid #e5e7eb}}
.date{{margin-left:auto;font-size:0.78em;color:#bbb}}
.minimap{{width:100%;aspect-ratio:1/1;border-radius:8px;margin:8px 0 2px;background:#e8eaed;z-index:0;cursor:pointer}}
.leaflet-container{{font:inherit;background:#e8eaed}}
.leaflet-div-icon{{background:transparent;border:none}}
.addr-cap{{text-align:center;font-size:0.84em;color:#444;font-weight:600;margin:0;padding:8px 14px 0}}
.hood-line{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}
.hood{{font-size:0.82em;font-weight:700;color:#0f766e}}
.utype{{font-size:0.68em;font-weight:700;text-transform:uppercase;letter-spacing:.04em;padding:1px 7px;border-radius:8px}}
.utype-house{{background:#fef3c7;color:#92400e;border:1px solid #fcd34d}}
.utype-apt{{background:#eef2ff;color:#3730a3;border:1px solid #c7d2fe}}
.amen-commute{{display:flex;gap:36px;margin:10px 0;align-items:flex-start}}
.ac-col{{display:flex;flex-direction:column}}
.ac-h{{font-size:0.66em;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#9ca3af;margin-bottom:7px}}
.amenities{{display:grid;grid-template-columns:1fr 1fr;gap:5px 10px}}
.am{{display:flex;align-items:center;gap:8px;height:20px;line-height:1}}
.am-ico{{font-size:1.15em;width:1.4em;text-align:center}}
.am b{{font-size:1.15em;font-weight:800}}
.am-yes b{{color:#16a34a}}
.am-no b{{color:#dc2626}}
.am-unk b{{color:#eab308}}
.commutes{{display:flex;flex-direction:column;gap:6px}}
.cm{{display:flex;align-items:center;gap:8px;height:20px;font-size:0.86em;font-weight:700;line-height:1}}
.cm-ico{{font-size:1.1em;width:1.4em;text-align:center}}
.cm-walk{{color:#16a34a}}
.cm-bike{{color:#16a34a}}
.cm-transit{{color:#7c3aed}}
.empty{{color:#aaa;font-style:italic;padding:12px 0}}
.pref-note{{background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:10px 14px;font-size:0.85em;color:#92400e;margin-bottom:20px}}
.links{{display:grid;grid-template-columns:1fr 1fr;gap:4px 24px;margin-bottom:8px}}
.links a{{color:#1d4ed8;font-size:0.88em;text-decoration:none;padding:3px 0}}
.links a:hover{{text-decoration:underline}}
table.hood{{width:100%;border-collapse:collapse;font-size:0.88em}}
table.hood th{{text-align:left;padding:8px 12px;background:#f3f4f6;font-size:0.78em;text-transform:uppercase;letter-spacing:.06em;color:#666}}
table.hood td{{padding:8px 12px;border-bottom:1px solid #e5e7eb}}
.score-stars{{color:#f59e0b}}
hr{{border:none;border-top:1px solid #e5e7eb;margin:24px 0}}
.footer{{font-size:0.78em;color:#bbb;margin-top:20px;text-align:center}}
.search-summary{{background:#fff;border-radius:10px;padding:16px 20px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:24px}}
.search-summary h2{{font-size:0.9em;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#555;margin-bottom:12px}}
table.ss{{width:100%;border-collapse:collapse;font-size:0.88em}}
table.ss th{{text-align:left;padding:6px 10px;background:#f3f4f6;font-size:0.78em;text-transform:uppercase;letter-spacing:.05em;color:#666}}
table.ss td{{padding:6px 10px;border-bottom:1px solid #f0f0f0}}
table.ss td.num{{text-align:right;font-variant-numeric:tabular-nums}}
table.ss td.new-count{{font-weight:700;color:#166534}}
.ss-url{{font-size:0.78em;color:#9ca3af;word-break:break-all}}
@media(max-width:640px){{.cards{{grid-template-columns:1fr}}.links{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<div class="page">
<h1>Apartment Search &mdash; Cambridge / Somerville</h1>
<p class="subtitle">Budget $2,000&ndash;$2,800/mo &nbsp;&middot;&nbsp; 1 Bedroom (whole unit) &nbsp;&middot;&nbsp; Move-in Sept 1 &nbsp;&middot;&nbsp; Commute: Kendall Square / Broad Institute &nbsp;&middot;&nbsp; {date}</p>
{search_summary}
{sections}
<hr>
<div class="section">
<div class="sec-header"><span class="sec-title">Search Links</span></div>
<div class="links">{links}</div>
</div>
<hr>
<div class="section">
<div class="sec-header"><span class="sec-title">Neighborhood Guide &mdash; Commute to Kendall</span></div>
<table class="hood">
<tr><th>Neighborhood</th><th>Transit to Kendall</th><th>Avg 1BR</th><th>Score</th></tr>
{neighborhoods}
</table>
</div>
<p class="footer">Generated by track.py &nbsp;&middot;&nbsp; {date}</p>
</div>
<script>
(function(){{
  if (typeof L === 'undefined') return;
  var io = new IntersectionObserver(function(entries){{
    entries.forEach(function(e){{
      if (!e.isIntersecting) return;
      var el = e.target;
      io.unobserve(el);
      if (el.dataset.inited) return;
      el.dataset.inited = '1';
      var lat = parseFloat(el.dataset.lat), lon = parseFloat(el.dataset.lon);
      if (isNaN(lat) || isNaN(lon)) return;
      var BROAD = [42.36266, -71.08644];
      var map = L.map(el, {{zoomControl:false, attributionControl:false, dragging:false,
        scrollWheelZoom:false, doubleClickZoom:false, boxZoom:false, keyboard:false,
        touchZoom:false, tap:false}});
      L.tileLayer('https://a.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}.png', {{maxZoom:19, subdomains:'abcd'}}).addTo(map);
      var approx = el.dataset.approx === '1';
      function pinIcon(color, op) {{
        return L.divIcon({{className:'pin', iconSize:[22,30], iconAnchor:[11,29],
          html:'<svg width="22" height="30" viewBox="0 0 22 30"><path d="M11 1C5.5 1 1 5.5 1 11c0 6.5 10 18 10 18s10-11.5 10-18C21 5.5 16.5 1 11 1z" fill="'+color+'" fill-opacity="'+op+'" stroke="#fff" stroke-width="1.5"/><circle cx="11" cy="11" r="3.5" fill="#fff"/></svg>'}});
      }}
      L.marker([lat, lon], {{icon: pinIcon('#dc2626', approx ? 0.5 : 1)}}).addTo(map);
      L.marker(BROAD, {{icon: pinIcon('#2563eb', 1)}}).bindTooltip('Broad Institute').addTo(map);
      var routes = {{}};
      try {{ routes = JSON.parse(el.dataset.routes || '{{}}'); }} catch (e) {{}}
      // Fit first so projection is valid, then draw.
      map.fitBounds([[lat, lon], BROAD], {{paddingTopLeft:[20, 28], paddingBottomRight:[20, 20], maxZoom:18}});
      // Same-width solid lines. Each is rigidly shifted by a few pixels
      // perpendicular to the listing→Broad direction, so shared-street routes
      // run parallel with a little whitespace. A uniform shift can't self-
      // intersect → no loops.
      var GAP = 3.2, W = 1.8;
      var pp0 = map.latLngToLayerPoint(L.latLng(lat, lon));
      var pp1 = map.latLngToLayerPoint(L.latLng(BROAD[0], BROAD[1]));
      var pdx = pp1.x - pp0.x, pdy = pp1.y - pp0.y;
      var plen = Math.hypot(pdx, pdy) || 1;
      var nx = -pdy / plen, ny = pdx / plen;   // unit perpendicular (px)
      function shift(pts, k) {{
        var ox = nx * GAP * k, oy = ny * GAP * k;
        return pts.map(function(p) {{
          var q = map.latLngToLayerPoint(L.latLng(p[0], p[1]));
          return map.layerPointToLatLng(L.point(q.x + ox, q.y + oy));
        }});
      }}
      function line(pts, color, k) {{
        if (pts && pts.length > 1)
          L.polyline(shift(pts, k), {{color:color, weight:W, opacity:1}}).addTo(map);
      }}
      function drawTransit(segs, color, k) {{
        if (!segs || !segs.length) return;
        if (Array.isArray(segs[0])) {{ line(segs, color, k); return; }}
        segs.forEach(function(s) {{ line(s.pts, color, k); }});
      }}
      line(routes.walk, '#16a34a', 0);               // 🚶 green walking route
      // fastest public-transit route in purple (only when it differs from walk)
      var tmode = el.dataset.tmode;
      if (tmode && routes[tmode]) drawTransit(routes[tmode], '#7c3aed', 0);
      el.title = approx ? 'Approximate location (geocoded from area)' : 'Exact location';
      el.addEventListener('click', function(){{
        window.open('https://www.openstreetmap.org/?mlat='+lat+'&mlon='+lon+'#map=16/'+lat+'/'+lon, '_blank');
      }});
    }});
  }}, {{rootMargin: '250px'}});
  document.querySelectorAll('.minimap').forEach(function(el){{ io.observe(el); }});

  // Street View pan on the grid: animate a 180° sweep (15° steps, pauses at the
  // ends) only while the card is on screen, to limit Static API requests.
  var svOffsets = []; for (var o = -90; o <= 90; o += 10) svOffsets.push(o);
  function svBaseUrl(src) {{ return src.replace(/&heading=\\d+/, ''); }}
  // fixed time per frame by section: slower middle third, faster outer thirds
  function svDwell(idx, n) {{ var p = idx/(n-1); return (p >= 1/3 && p <= 2/3) ? 300 : 190; }}
  function startSV(img) {{
    if (img.dataset.svAnim) return;
    var base = parseInt(img.dataset.svbase, 10);
    if (isNaN(base)) return;
    img.dataset.svAnim = '1';
    var root = svBaseUrl(img.getAttribute('src'));
    var heads = svOffsets.map(function(o) {{ return (base + o + 360) % 360; }});
    heads.forEach(function(h) {{ var p = new Image(); p.src = root + '&heading=' + h; }});
    var i = (heads.length - 1) >> 1, dir = 1;
    function step() {{
      i += dir;
      if (i >= heads.length) {{ i = heads.length - 1; dir = -1; }}
      else if (i < 0) {{ i = 0; dir = 1; }}
      img.src = root + '&heading=' + heads[i];
      img._svT = setTimeout(step, svDwell(i, heads.length));
    }}
    img._svT = setTimeout(step, svDwell(i, heads.length));
  }}
  function stopSV(img) {{
    if (img._svT) {{ clearTimeout(img._svT); img._svT = null; }}
    img.dataset.svAnim = '';
  }}
  var svio = new IntersectionObserver(function(entries) {{
    entries.forEach(function(e) {{
      if (e.isIntersecting) startSV(e.target); else stopSV(e.target);
    }});
  }}, {{rootMargin: '0px', threshold: 0.4}});
  document.querySelectorAll('img.streetview[data-svbase]').forEach(function(img){{ svio.observe(img); }});
}})();
</script>
</body>
</html>
"""

# ── Database ──────────────────────────────────────────────────────────────────

def _load_turso():
    """Return (url, token) for the shared remote DB, or None if not configured.

    Looks at TURSO_DATABASE_URL / TURSO_AUTH_TOKEN env vars first, then a
    gitignored .turso_key file (KEY=VALUE lines). When unset, the app falls
    back to the local listings.db file so you can still work offline."""
    url   = os.environ.get("TURSO_DATABASE_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if (not url or not token) and os.path.exists(TURSO_KEY_PATH):
        with open(TURSO_KEY_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().upper()
                v = v.strip().strip('"').strip("'")
                if not url and k in ("TURSO_DATABASE_URL", "DATABASE_URL", "URL"):
                    url = v
                elif not token and k in ("TURSO_AUTH_TOKEN", "AUTH_TOKEN", "TOKEN"):
                    token = v
    return (url, token) if url and token else None


# sqlite3.Row-compatible adapters for the libSQL (Turso) driver. libSQL returns
# plain tuples and has no row_factory, so these give the rest of the codebase the
# r["col"] / r[int] / dict(r) access it already expects from sqlite3.Row.
class _Row:
    __slots__ = ("_cols", "_vals", "_map")
    def __init__(self, cols, vals):
        self._cols, self._vals = cols, vals
        self._map = {c: i for i, c in enumerate(cols)}
    def __getitem__(self, k):
        return self._vals[self._map[k]] if isinstance(k, str) else self._vals[k]
    def keys(self):
        return list(self._cols)
    def get(self, k, default=None):
        i = self._map.get(k)
        return default if i is None else self._vals[i]
    def __contains__(self, k):
        return k in self._map
    def __iter__(self):
        return iter(self._vals)
    def __len__(self):
        return len(self._vals)


class _Cursor:
    def __init__(self, cur):
        self._c = cur
    @property
    def lastrowid(self):
        return self._c.lastrowid
    @property
    def description(self):
        return self._c.description
    def _cols(self):
        return [d[0] for d in (self._c.description or [])]
    def fetchone(self):
        r = self._c.fetchone()
        return None if r is None else _Row(self._cols(), r)
    def fetchall(self):
        cols = self._cols()
        return [_Row(cols, r) for r in self._c.fetchall()]
    def __iter__(self):
        return iter(self.fetchall())


class _Conn:
    """Thin wrapper exposing the sqlite3 connection API over a libSQL conn."""
    def __init__(self, raw):
        self._raw = raw
    def execute(self, sql, params=None):
        return _Cursor(self._raw.execute(sql, params or ()))
    def executescript(self, sql):
        return self._raw.executescript(sql)
    def commit(self):
        return self._raw.commit()
    def cursor(self):
        return _Cursor(self._raw.cursor())
    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass


# (column, type) pairs added incrementally over the life of the schema.
_EXTRA_COLUMNS = [
    ("source",       "TEXT DEFAULT ''"),
    ("is_house",     "INTEGER DEFAULT 0"),
    ("image",        "TEXT DEFAULT ''"),
    ("has_laundry",  "INTEGER DEFAULT 0"),
    ("available",    "TEXT DEFAULT ''"),
    ("lat",          "REAL"),
    ("lon",          "REAL"),
    ("geo_src",      "TEXT DEFAULT ''"),
    ("walk_min",     "INTEGER"),
    ("bike_min",     "INTEGER"),
    ("transit_min",  "INTEGER"),
    ("bus_min",      "INTEGER"),
    ("neighborhood", "TEXT"),
    ("meta",         "TEXT"),
    ("amenities",    "TEXT"),
    ("route_geo",    "TEXT"),
    ("listed_on",    "TEXT"),
    ("rating",       "TEXT DEFAULT ''"),   # '', 'no', 'mid', 'nice' (user swipe rating)
    ("delisted",     "INTEGER DEFAULT 0"), # 1 = removed by author / no longer available
    ("delisted_on",  "TEXT"),              # date we detected the removal
    ("beds",         "REAL"),              # bedrooms (from listing detail page)
    ("baths",        "REAL"),              # bathrooms
    ("sqft",         "INTEGER"),           # square feet
    ("photos",       "TEXT"),              # JSON array of all listing photo URLs
    ("amen_text",    "TEXT"),              # raw amenities text scraped from detail pages
    ("sv_heading",   "INTEGER"),           # Street View heading (deg) facing the building
    ("duplicate",    "INTEGER DEFAULT 0"), # 1 = same address as another (kept) listing
    ("dup_of",       "INTEGER"),           # id of the canonical listing it duplicates
]

# Allowed user ratings, worst → best. Emoji + label drive the swipe + card buttons.
RATINGS = ["hmm", "ok", "love"]
RATING_EMOJI = {"hmm": "🤔", "ok": "😊", "love": "😍"}


def _db_init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT NOT NULL,
            title       TEXT,
            price       INTEGER,
            location    TEXT,
            status      TEXT DEFAULT 'new',
            notes       TEXT DEFAULT '',
            source      TEXT DEFAULT '',
            fingerprint TEXT,
            added_on    TEXT NOT NULL,
            updated_on  TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_url ON listings(url);
        CREATE INDEX IF NOT EXISTS idx_fingerprint ON listings(fingerprint);
        CREATE TABLE IF NOT EXISTS scrape_runs (
            source      TEXT PRIMARY KEY,
            last_run    TEXT NOT NULL,
            last_total  INTEGER DEFAULT 0,
            last_new    INTEGER DEFAULT 0,
            last_error  TEXT DEFAULT ''
        );
    """)
    conn.commit()
    # Only ALTER for columns that don't exist yet — avoids a flurry of failing
    # round-trips to the remote DB on every connect.
    existing = {r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()}
    for name, decl in _EXTRA_COLUMNS:
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE listings ADD COLUMN {name} {decl}")
            except Exception:
                pass
    conn.commit()


def db_connect():
    creds = _load_turso()
    if creds:
        import libsql  # provided by the venv; only needed when sharing is on
        url, token = creds
        conn = _Conn(libsql.connect(url, auth_token=token))
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    _db_init_schema(conn)
    return conn


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def ask(prompt, default=""):
    """input() that won't crash on non-interactive stdin (returns default)."""
    try:
        return input(prompt)
    except EOFError:
        print(f"{default or '(no input)'}  [non-interactive; using default]")
        return default


# ── Deduplication ─────────────────────────────────────────────────────────────

_STRIP_RE = re.compile(r'[\W_]+')

def normalize(text):
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r'\$[\d,]+', '', t)
    t = re.sub(r'\b(br|bd|bed|bedroom|bath|ba|apt|apartment|unit|avail|available|rent|rental|sq|ft|sqft)\b', '', t)
    t = _STRIP_RE.sub(' ', t).strip()
    return t


def fingerprint(title, location, price):
    norm = normalize(str(title or "")) + "|" + normalize(str(location or "")) + "|" + str(price or "")
    return hashlib.md5(norm.encode()).hexdigest()[:12]


def is_duplicate(conn, url, fp):
    row = conn.execute("SELECT id FROM listings WHERE url = ?", (url,)).fetchone()
    if row:
        return True, row["id"]
    if fp:
        row = conn.execute("SELECT id FROM listings WHERE fingerprint = ?", (fp,)).fetchone()
        if row:
            return True, row["id"]
    return False, None


# ── Craigslist scraper ────────────────────────────────────────────────────────

class _CLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._cur = None
        self._capture = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "")

        if tag == "li" and "cl-search-result" in cls:
            self._cur = {"title": "", "url": "", "price": "", "location": "", "date": ""}
            return

        if self._cur is None:
            return

        if tag == "a" and ("cl-app-anchor" in cls or "posting-title" in cls):
            href = a.get("href", "")
            if href and not self._cur["url"]:
                self._cur["url"] = href if href.startswith("http") else "https://boston.craigslist.org" + href
        elif tag == "span" and "label" in cls:
            self._capture = "title"
        elif tag == "span" and "priceinfo" in cls:
            self._capture = "price"
        elif tag in ("span", "div") and ("meta" in cls or "separator" in cls):
            self._capture = None
        elif tag == "div" and "supertitle" in cls:
            self._capture = "location"
        elif tag == "div" and "posting-title" in cls:
            self._capture = "title"
        elif tag == "time":
            dt = a.get("datetime", "")
            if dt and self._cur:
                self._cur["date"] = dt[:10]

    def handle_endtag(self, tag):
        if tag in ("span", "div", "a"):
            self._capture = None
        if tag == "li" and self._cur is not None:
            if self._cur.get("url"):
                self.results.append(self._cur)
            self._cur = None

    def handle_data(self, data):
        if self._cur and self._capture:
            self._cur[self._capture] = (self._cur[self._capture] + " " + data).strip()


def _has_playwright():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


async def _lazy_scroll(page, steps=20, pause=250):
    """Scroll incrementally so lazy-loaded images actually fetch.
    window.scrollBy drives both Craigslist's gallery and apartments.com/FB;
    a single jump to the bottom skips images between top and bottom."""
    for _ in range(steps):
        await page.evaluate("window.scrollBy(0, 900)")
        await page.wait_for_timeout(pause)
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(600)


async def _img_url(el):
    """Best-effort real image URL from a card/anchor element.
    Ignores data: placeholder pixels that sites use before lazy-load fires."""
    img = await el.query_selector("img")
    if not img:
        return ""
    # currentSrc reflects what actually loaded; src may still be a placeholder
    for prop in ("currentSrc", "src"):
        try:
            v = await img.evaluate(f"e => e.{prop}")
        except Exception:
            v = None
        if v and not v.startswith("data:"):
            return v
    for attr in ("data-src", "srcset"):
        v = await img.get_attribute(attr)
        if v and not v.startswith("data:"):
            return v.split()[0] if attr == "srcset" else v
    return ""


async def _scrape_cl_pw(url):
    """Scrape one CL search URL with Playwright. Returns list of listing dicts.

    Current CL markup (2026): results are <div class="cl-search-result"> with
    a.posting-title (title + href), .priceinfo, and .meta whose lines are
    [time-ago, beds/sqft, location].
    """
    from playwright.async_api import async_playwright
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=25000)
            await page.wait_for_selector(".cl-search-result", timeout=8000)
        except Exception:
            pass

        # scroll to load all lazy results (CL infinite-scrolls its gallery)
        await _lazy_scroll(page)

        cards = await page.query_selector_all(".cl-search-result")
        for card in cards:
            try:
                a = await card.query_selector("a.posting-title, a.cl-app-anchor")
                if not a:
                    continue
                href = await a.get_attribute("href") or ""
                if not href:
                    continue
                title = (await a.inner_text()).strip()

                price_el  = await card.query_selector(".priceinfo")
                price_raw = (await price_el.inner_text()).strip() if price_el else ""

                meta_el  = await card.query_selector(".meta")
                location, date = "", ""
                if meta_el:
                    lines = [ln.strip() for ln in (await meta_el.inner_text()).splitlines() if ln.strip()]
                    if lines:
                        date = lines[0]                      # relative, e.g. "<1hr ago", "6/4"
                        if len(lines) >= 2:
                            location = lines[-1]             # last line is neighborhood
                        # guard: location line shouldn't be the beds/sqft line
                        if re.fullmatch(r'[\d\s]*br[\d\s]*(ft2)?[\d\s]*', location.replace(",", "")):
                            location = ""

                rec = {
                    "title":    title,
                    "url":      href if href.startswith("http") else "https://boston.craigslist.org" + href,
                    "price":    price_raw,
                    "location": location,
                    "date":     date,
                    "image":    "",
                }
                # CL resets images to placeholders when off-screen (IntersectionObserver),
                # so capture the photo only after scrolling the card into view. Do this
                # just for the listings we'll keep, to avoid 200 needless scrolls.
                if cl_keep(rec):
                    try:
                        await card.scroll_into_view_if_needed(timeout=3000)
                        await page.wait_for_timeout(70)
                        rec["image"] = await _img_url(card)
                    except Exception:
                        pass
                results.append(rec)
            except Exception:
                continue

        await browser.close()
    return results


_CL_TIME_RE  = re.compile(r'<time class="date timeago" datetime="(\d{4}-\d{2}-\d{2})')
_CL_AVAIL_RE = re.compile(r'available_after["\']?\s*:\s*["\']?(\d{4})-(\d{2})-(\d{2})')
_MONTHS = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def _cl_detail(url):
    """(listed_on 'YYYY-MM-DD', available 'Mon D') from a LIVE Craigslist page.
    (None, None) if the page is gone/blocked or the markers are absent."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent":
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read()
        try:
            html = gzip.decompress(raw).decode("utf-8", "replace")
        except Exception:
            html = raw.decode("utf-8", "replace")
    except Exception:
        return None, None
    listed = None
    m = _CL_TIME_RE.search(html)
    if m:
        listed = m.group(1)
    avail = None
    m = _CL_AVAIL_RE.search(html)
    if m:
        mo, d = int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12:
            avail = f"{_MONTHS[mo]} {d}"
    return listed, avail


def scrape_craigslist():
    if _has_playwright():
        try:
            # Search ALL housing types. CL's house/duplex/townhouse filter is
            # useless here: nearly all Cambridge/Somerville house-floor rentals
            # (triple-deckers etc.) are posted as generic "apartment" type, so
            # the filter dropped ~48 of 49 real listings. We instead pull every
            # type and flag house units by keyword via _enrich()/house_score().
            results = asyncio.run(_scrape_cl_pw(SEARCH_URL_ALL))

            # keep only Cambridge/Somerville-area listings; drop rooms in shared units
            total = len(results)
            results = [r for r in results if cl_keep(r)]
            kept_area = len(results)
            results = [r for r in results if not is_shared(r.get("title") or "")]
            print(f"  [CL] {total} fetched (all types), {kept_area} in Cambridge/Somerville, "
                  f"{kept_area - len(results)} shared-room listings dropped")

            for r in results:
                p = re.sub(r'[^\d]', '', r.get("price", ""))
                r["price_int"] = int(p) if p else None
                r["source"] = "craigslist"
                r["_laundry"] = laundry_score(r.get("title") or "")
                r["_avail"]   = "9/1" if sept_score(r.get("title") or "") else ""
            # detail-page enrichment: real post date + availability (live pages only)
            print(f"  [CL] fetching detail pages for post date + availability "
                  f"({len(results)} listings)...")
            got_date = got_avail = 0
            for r in results:
                listed, avail = _cl_detail(r["url"])
                if listed:
                    r["_listed_on"] = listed
                    got_date += 1
                if avail:
                    r["_avail"] = avail
                    got_avail += 1
                _time.sleep(0.4)
            print(f"  [CL] detail: {got_date} post dates, {got_avail} availability dates")
            return results
        except Exception as e:
            print(f"  [!] Playwright fetch failed ({e}), falling back to urllib")

    # urllib fallback (CL usually 403s this — kept as a last resort)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }
    try:
        req = urllib.request.Request(SEARCH_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding", "") == "gzip":
                raw = gzip.decompress(raw)
            html = raw.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [!] Fetch failed: {e}")
        return []

    parser = _CLParser()
    parser.feed(html)
    results = parser.results

    if not results:
        urls = re.findall(r'(https://boston\.craigslist\.org/[a-z]+/apa/\d+\.html)', html)
        prices = re.findall(r'\$(\d[\d,]+)', html)
        for i, url in enumerate(urls[:30]):
            results.append({
                "url":      url,
                "title":    "",
                "price":    f"${prices[i]}" if i < len(prices) else "",
                "location": "",
                "date":     "",
            })

    for r in results:
        p = re.sub(r'[^\d]', '', r.get("price", ""))
        r["price_int"] = int(p) if p else None
        r["source"] = "craigslist"

    return results


# ── Apartments.com scraper ────────────────────────────────────────────────────

async def _scrape_apts_pw():
    """Scrape Apartments.com search pages.

    Notes (verified 2026-06):
    - Akamai blocks headless browsers with "Access Denied" → must run headed.
    - Filters must be encoded in the URL path; query params are ignored.
    - Cards are <article class="placard"> with .property-title and
      .property-address; price appears in the card text as $X,XXX.
    - Consecutive navigations occasionally get a transient Akamai denial →
      retry each URL up to 3× with a pause.
    """
    from playwright.async_api import async_playwright

    results = []
    async with async_playwright() as p:
        for label, url, is_house in APTS_URLS:
            print(f"  [apts] Loading {label}...")
            count_before = len(results)

            # Fresh browser per URL: Akamai flags the 2nd+ navigation in a
            # session, but a clean launch passes reliably.
            for attempt in range(3):
                browser = await p.chromium.launch(headless=False)
                try:
                    ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
                    page = await ctx.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(4000)

                    title = await page.title()
                    if "access denied" in title.lower():
                        print(f"         attempt {attempt+1}: Akamai denied, retrying with fresh browser...")
                        await browser.close()
                        await asyncio.sleep(6)
                        continue

                    # scroll to trigger lazy loading of cards + images
                    await _lazy_scroll(page)

                    cards = await page.query_selector_all("article.placard")
                    for card in cards:
                        try:
                            t_el = await card.query_selector(".property-title, .js-placardTitle")
                            a_el = await card.query_selector("a.property-link[href], a[href]")
                            d_el = await card.query_selector(".property-address")
                            if not a_el:
                                continue
                            href = await a_el.get_attribute("href") or ""
                            if not href:
                                continue
                            link_url = href if href.startswith("http") else "https://www.apartments.com" + href

                            card_title = (await t_el.inner_text()).strip() if t_el else ""
                            location   = (await d_el.inner_text()).strip() if d_el else ""

                            image = await _img_url(card)

                            text     = await card.inner_text()
                            price_m  = re.search(r'\$(\d[\d,]+)', text)
                            price_int = int(price_m.group(1).replace(",", "")) if price_m else None

                            # amenities live in the card text, e.g. "In Unit Washer & Dryer"
                            avail_m = re.search(r'\bAvailable\s+(Now|[A-Z][a-z]{2,8}\.?\s*\d{0,2})', text)

                            if card_title or price_int:
                                results.append({
                                    "url":       link_url,
                                    "title":     card_title,
                                    "price":     price_m.group(0) if price_m else "",
                                    "price_int": price_int,
                                    "location":  location,
                                    "date":      "",
                                    "source":    "apartments",
                                    "_house":    is_house,
                                    "image":     image or "",
                                    "_laundry":  laundry_score(text),
                                    "_avail":    avail_m.group(1).strip() if avail_m else "",
                                })
                        except Exception:
                            continue
                    await browser.close()
                    break  # success — stop retrying this URL
                except Exception as e:
                    print(f"         attempt {attempt+1} failed: {e}")
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    await asyncio.sleep(4)

            print(f"         → {len(results) - count_before} from this page")
            await asyncio.sleep(2)

    # deduplicate by URL; drop nearby-town results (the address field is reliable)
    # and rooms in shared units (hard constraint)
    seen, unique = set(), []
    for r in results:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        if not in_camb_som(r.get("location") or r.get("title") or ""):
            continue
        if is_shared(r.get("title") or ""):
            continue
        unique.append(r)
    return unique


def _apts_detail_parse(html):
    """Pull beds/baths/sqft/availability from an Apartments.com listing detail
    page. The unit facts live in repeated
    <p class="rentInfoLabel">X</p><p class="rentInfoDetail">Y</p> pairs."""
    pairs = {k.strip(): v.strip() for k, v in re.findall(
        r'rentInfoLabel">([^<]+)</p>\s*<p class="rentInfoDetail">([^<]*)</p>', html)}

    def _num(s):
        m = re.search(r'[\d.]+', s or "")
        return float(m.group(0)) if m else None

    sqm = re.search(r'([\d,]+)\s*sq', pairs.get("Square Feet", ""))

    # amenities/features section (rich list: Dishwasher, High Speed Internet,
    # Heating Available, laundry, etc.) → stored as amen_text for row_amenities
    amen_text = ""
    am = re.search(r'amenitiesSectionV2.*?(?=mls-feature-section|class="mapSection|</body)', html, re.S)
    if am:
        amen_text = re.sub(r'<[^>]+>', ' ', am.group(0))
        amen_text = re.sub(r'\s+', ' ', amen_text).strip()[:2500]

    # gallery photos (dedupe, keep order, cap)
    photos = list(dict.fromkeys(
        re.findall(r'https://images1\.apartments\.com/i2/[^"\'\s)]+', html)))[:24]

    return {
        "beds":  _num(pairs.get("Bedrooms")),
        "baths": _num(pairs.get("Bathrooms")),
        "sqft":  int(sqm.group(1).replace(",", "")) if sqm else None,
        "avail": parse_move_in(pairs.get("Available", "")),
        "amen_text": amen_text,
        "photos": photos,
    }


async def _enrich_apts_details_pw(urls, log=print):
    """Headed-fetch each Apartments.com detail page and return {url: {beds, baths,
    sqft, avail}}. Akamai allows sequential navigations in one browser if paced,
    so we reuse a single window. Blocked/failed pages are skipped (not in result)."""
    from playwright.async_api import async_playwright
    out = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False, args=["--disable-blink-features=AutomationControlled"])
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        consecutive_blocks = 0
        try:
            for i, u in enumerate(urls):
                try:
                    await page.goto(u, wait_until="domcontentloaded", timeout=40000)
                    await page.wait_for_timeout(2500)
                    if "access denied" in (await page.title()).lower():
                        consecutive_blocks += 1
                        log(f"  [{i+1}/{len(urls)}] blocked ({consecutive_blocks})")
                        if consecutive_blocks >= 5:
                            log(f"  Akamai throttling — stopping after {len(out)} enriched; "
                                f"re-run later to finish the rest.")
                            break
                        await page.wait_for_timeout(4000)
                        continue
                    consecutive_blocks = 0
                    info = _apts_detail_parse(await page.content())
                    if (info["beds"] is not None or info["sqft"] is not None
                            or info["avail"] or info["amen_text"] or info["photos"]):
                        out[u] = info
                        log(f"  [{i+1}/{len(urls)}] beds={info['beds']} baths={info['baths']} "
                            f"sqft={info['sqft']} avail={info['avail'] or '—'} "
                            f"photos={len(info['photos'])}")
                except Exception as e:
                    log(f"  [{i+1}/{len(urls)}] error: {e}")
                await page.wait_for_timeout(1200)
        finally:
            await browser.close()
    return out


def enrich_apts_details(conn, log=print, only_missing=True):
    """Fill beds/baths/sqft (and move-in) for Apartments.com listings from their
    detail pages. Returns the number of rows updated."""
    where = "AND sqft IS NULL" if only_missing else ""
    rows = conn.execute(
        f"SELECT id, url FROM listings WHERE source='apartments' "
        f"AND COALESCE(delisted,0)=0 {where}").fetchall()
    if not rows:
        log("  no Apartments.com listings need detail enrichment")
        return 0
    log(f"  fetching detail pages for {len(rows)} Apartments.com listing(s)...")
    data = asyncio.run(_enrich_apts_details_pw([r["url"] for r in rows], log=log))
    n = 0
    for r in rows:
        d = data.get(r["url"])
        if not d:
            continue
        conn.execute(
            "UPDATE listings SET beds=?, baths=?, sqft=?, "
            "available=COALESCE(NULLIF(?,''), available), "
            "amen_text=COALESCE(NULLIF(?,''), amen_text), "
            "photos=COALESCE(NULLIF(?,''), photos), updated_on=? WHERE id=?",
            (d["beds"], d["baths"], d["sqft"], d["avail"] or "",
             d["amen_text"] or "", _json.dumps(d["photos"]) if d["photos"] else "",
             now(), r["id"]))
        n += 1
    conn.commit()
    log(f"  updated {n} listing(s) with beds/baths/sqft/amenities/photos")
    return n


# ── Zillow scraper ────────────────────────────────────────────────────────────

ZILLOW_PROFILE_DIR = os.path.join(SCRIPT_DIR, ".zillow_profile")

# _mp = monthly payment path token; beds in path. Client-side price check too,
# in case Zillow ignores/redirects the _mp segment.
ZILLOW_URLS = [
    ("Cambridge 1BR",  "https://www.zillow.com/cambridge-ma/rentals/1-_beds/2000-2800_mp/"),
    ("Somerville 1BR", "https://www.zillow.com/somerville-ma/rentals/1-_beds/2000-2800_mp/"),
]


# Rent.com: bedrooms=1 is honored; price filtering is path-based and unreliable,
# so we pass only bedrooms and budget-filter the 1BR floor plans client-side.
RENT_URLS = [
    ("Cambridge 1BR",  "https://www.rent.com/massachusetts/cambridge-apartments?bedrooms=1"),
    ("Somerville 1BR", "https://www.rent.com/massachusetts/somerville-apartments?bedrooms=1"),
]


def scrape_rent():
    """Scrape Rent.com via the embedded __NEXT_DATA__ JSON.

    Rent.com serves the full listing payload in the initial HTML (no bot wall on
    a plain GET as of 2026-06), so a simple urllib fetch is enough — no headed
    browser needed. Listings are building/complex cards: each has location
    lat/lng and a floorPlans[] list; we extract the cheapest 1-bedroom floor
    plan and budget-filter to <= 2800 client-side (the URL price filter is
    path-based and not reliably honored).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }

    results = []
    for label, url in RENT_URLS:
        print(f"  [rent] Loading {label}...")
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "") == "gzip":
                    raw = gzip.decompress(raw)
                html = raw.decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  [rent] Failed loading {label}: {e}")
            continue

        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html, re.S,
        )
        if not m:
            print(f"  [rent] No data payload for {label} (page layout changed?)")
            continue
        try:
            data = _json.loads(m.group(1))
            items = (data.get("props", {}).get("pageProps", {}).get("pageData", {})
                     .get("location", {}).get("listingSearch", {}).get("listings", []))
        except Exception as e:
            print(f"  [rent] Could not parse payload for {label}: {e}")
            continue

        for it in items:
            try:
                path = it.get("urlPathname") or ""
                if not path:
                    continue
                full_url = path if path.startswith("http") else "https://www.rent.com" + path

                # cheapest 1-bedroom floor plan (take its price + move-in date)
                one_br = [fp for fp in (it.get("floorPlans") or [])
                          if fp.get("bedCount") == 1
                          and (fp.get("priceRange") or {}).get("min")]
                if not one_br:
                    continue  # no priced 1BR unit — can't budget-check, drop
                cheapest = min(one_br, key=lambda fp: fp["priceRange"]["min"])
                price_int = cheapest["priceRange"]["min"]

                loc  = it.get("location") or {}
                city = loc.get("city") or ""
                name = it.get("name") or city
                location = ", ".join(x for x in (city, loc.get("stateAbbr")) if x)

                results.append({
                    "url":       full_url,
                    "title":     name,
                    "price":     f"${price_int:,}",
                    "price_int": price_int,
                    "location":  location,
                    "date":      "",
                    "source":    "rent",
                    "image":     "",
                    "_lat":      loc.get("lat"),
                    "_lon":      loc.get("lng"),
                    "_laundry":  0,   # not exposed on search cards
                    # move-in from the floor plan's availability; Rent.com doesn't
                    # expose an original-listing date, so _listed_on stays blank.
                    "_avail":    cheapest.get("availableDate") or "",
                })
            except Exception:
                continue
        print(f"         → {len(items)} on page")

    # dedupe + area/shared/budget filters
    seen, unique = set(), []
    for r in results:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        if not in_camb_som(r.get("location") or ""):
            continue
        if is_shared(r.get("title") or ""):
            continue
        if r["price_int"] > 2800:
            continue
        unique.append(r)
    return unique


async def _scrape_zillow_pw():
    """Scrape Zillow rentals via the embedded __NEXT_DATA__ JSON.

    Notes (verified 2026-06):
    - PerimeterX blocks repeated automated visits ("Access to this page has
      been denied") and may show a Press & Hold captcha. We use a persistent
      profile (.zillow_profile): solve the captcha once in the headed window
      and the clearance cookie persists for future runs.
    - DOM card selectors are unstable; #__NEXT_DATA__ → props.pageProps.
      searchPageState.cat1.searchResults.listResults is the reliable source,
      and includes exact lat/long per listing (no geocoding needed).
    """
    from playwright.async_api import async_playwright

    results = []
    async with async_playwright() as p:
        for label, url in ZILLOW_URLS:
            print(f"  [zillow] Loading {label}...")
            ctx = await p.chromium.launch_persistent_context(
                ZILLOW_PROFILE_DIR,
                headless=False,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                for attempt in range(3):
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(5000)
                    title = (await page.title()).lower()

                    if "denied" in title or "captcha" in title:
                        if sys.stdin.isatty():
                            print("  [zillow] Bot check — solve the 'Press & Hold' in the "
                                  "browser window, then press Enter here...")
                            ask("")
                            continue  # retry same URL with clearance cookie
                        print("  [zillow] Blocked by bot detection. Run "
                              "`python3 track.py fetch-zillow` in a terminal once to "
                              "solve the captcha; the clearance persists in .zillow_profile.")
                        break

                    nd = await page.query_selector("#__NEXT_DATA__")
                    if not nd:
                        print(f"  [zillow] No data payload on attempt {attempt+1}")
                        continue
                    data = _json.loads(await nd.inner_text())
                    items = (data.get("props", {}).get("pageProps", {})
                             .get("searchPageState", {}).get("cat1", {})
                             .get("searchResults", {}).get("listResults", []))

                    for item in items:
                        try:
                            addr      = item.get("address") or ""
                            price_raw = item.get("price") or ""
                            try:
                                price_int = int(item.get("unformattedPrice") or 0) or None
                            except (TypeError, ValueError):
                                price_int = None
                            if price_int is None:
                                m = re.search(r'\$(\d[\d,]+)', price_raw)
                                price_int = int(m.group(1).replace(",", "")) if m else None
                            if price_int is None:
                                # building cards keep prices per-unit: units=[{price,beds}]
                                for u in (item.get("units") or []):
                                    if str(u.get("beds")) == "1":
                                        m = re.search(r'\$(\d[\d,]+)', u.get("price") or "")
                                        if m:
                                            price_int = int(m.group(1).replace(",", ""))
                                            price_raw = u.get("price") or price_raw
                                            break

                            detail = item.get("detailUrl") or ""
                            if not detail:
                                continue
                            full_url = detail if detail.startswith("http") else "https://www.zillow.com" + detail

                            ll = item.get("latLong") or {}
                            results.append({
                                "url":       full_url,
                                "title":     addr,
                                "price":     price_raw,
                                "price_int": price_int,
                                "location":  addr,
                                "date":      "",
                                "source":    "zillow",
                                "image":     item.get("imgSrc") or "",
                                "_lat":      ll.get("latitude"),
                                "_lon":      ll.get("longitude"),
                                "_laundry":  0,   # not exposed on search cards
                                "_avail":    "",
                            })
                        except Exception:
                            continue
                    print(f"         → {len(items)} on page")
                    break
            except Exception as e:
                print(f"  [zillow] Failed loading {label}: {e}")
            finally:
                await ctx.close()
            await asyncio.sleep(3)

    # dedupe + area/shared/price filters
    seen, unique = set(), []
    for r in results:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        if not in_camb_som(r.get("location") or ""):
            continue
        if is_shared(r.get("title") or ""):
            continue
        if not r.get("price_int"):
            continue  # unpriced cards can't be budget-checked — drop
        if r["price_int"] > 2800:
            continue
        unique.append(r)
    return unique


# ── HotPads scraper ───────────────────────────────────────────────────────────

HOTPADS_PROFILE_DIR = os.path.join(SCRIPT_DIR, ".hotpads_profile")

# Tight lat/lon box covering Cambridge + Somerville (the byCoordsV2 API is
# bounding-box based; we still filter to Camb/Som by city client-side, since the
# box bleeds into Medford/Arlington/Boston at the edges).
HOTPADS_BBOX = {
    "lat": 42.385, "lon": -71.12,
    "minLat": 42.355, "maxLat": 42.418, "minLon": -71.17, "maxLon": -71.075,
}
HOTPADS_SEARCH_URL = (
    "https://hotpads.com/cambridge-ma/apartments-for-rent?beds=1-1&price=2000-2800"
)
# propertyType values that count as a house/townhouse-style unit (preference).
_HOTPADS_HOUSE_TYPES = {"house", "townhouse", "divided"}


def _hotpads_api_url(offset, limit):
    bb = HOTPADS_BBOX
    return (
        "https://hotpads.com/hotpads-api/api/v2/listing/byCoordsV2?"
        "orderBy=score&searchSlug=apartments-for-rent&lowPrice=2000&highPrice=2800"
        "&bedrooms=1"
        "&bathrooms=0,0.5,1,1.5,2,2.5,3,3.5,4,4.5,5,5.5,6,6.5,7,7.5,8plus"
        "&propertyTypes=condo,divided,garden,house,large,medium,townhouse"
        "&listingTypes=rental,sublet,corporate"
        "&includePhotosCollection=true"
        f"&lat={bb['lat']}&lon={bb['lon']}"
        f"&maxLat={bb['maxLat']}&maxLon={bb['maxLon']}"
        f"&minLat={bb['minLat']}&minLon={bb['minLon']}"
        f"&offset={offset}&limit={limit}&components=basic,model"
    )


def _hotpads_one_br_price(lst):
    """Cheapest 1-bedroom price for a HotPads listing, or None."""
    ms = [m for m in (lst.get("models") or [])
          if m.get("numBeds") == 1 and m.get("lowPrice")]
    if ms:
        return int(min(m["lowPrice"] for m in ms))
    summ = lst.get("modelSummary") or {}
    if summ.get("minBeds") == 1 and summ.get("minPrice"):
        return int(summ["minPrice"])
    return None


async def _scrape_hotpads_pw():
    """Scrape HotPads via its internal byCoordsV2 listing API.

    Notes (verified 2026-06):
    - HotPads is a Zillow property and uses the same PerimeterX bot wall: a plain
      GET 403s ("Access to this page has been denied"). We use a persistent
      profile (.hotpads_profile) and a headed browser; solve any captcha once and
      the clearance cookie persists for future runs.
    - The SPA fetches listing cards from /hotpads-api/api/v2/listing/byCoordsV2
      (bounding-box + filters). We call it directly from the authenticated page
      context and parse data.buildings[].listings[]: each listing has an
      uriMalone (detail URL), address, building geo (lat/lon), and a models[]
      list of per-bedroom {numBeds, lowPrice, highPrice} we use for the 1BR price.
    """
    from playwright.async_api import async_playwright

    results = []
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            HOTPADS_PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            # Warm up so PerimeterX cookies / referer are valid for the API call.
            for attempt in range(3):
                await page.goto(HOTPADS_SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(4000)
                title = (await page.title()).lower()
                if "denied" in title or "captcha" in title:
                    if sys.stdin.isatty():
                        print("  [hotpads] Bot check — solve the captcha in the browser "
                              "window, then press Enter here...")
                        ask("")
                        continue
                    print("  [hotpads] Blocked by bot detection. Run "
                          "`python3 track.py fetch-hotpads` in a terminal once to solve "
                          "the captcha; the clearance persists in .hotpads_profile.")
                    await ctx.close()
                    return []
                break

            # Paginate the API (limit 80 honored; ~350 in box → up to 5 pages).
            offset, limit = 0, 80
            for _ in range(6):
                resp = await page.request.get(_hotpads_api_url(offset, limit))
                if resp.status != 200:
                    print(f"  [hotpads] API returned {resp.status} at offset {offset}")
                    break
                data = (await resp.json()).get("data", {}) or {}
                buildings = data.get("buildings", []) or []
                if not buildings:
                    break

                for b in buildings:
                    geo = b.get("geo") or {}
                    for lst in b.get("listings", []):
                        try:
                            uri = lst.get("uriMalone") or ""
                            if not uri:
                                continue
                            full_url = uri if uri.startswith("http") else "https://hotpads.com" + uri

                            price_int = _hotpads_one_br_price(lst)
                            if not price_int:
                                continue  # no priced 1BR unit — can't budget-check

                            addr = lst.get("address") or {}
                            city = addr.get("city") or ""
                            street = "" if addr.get("hideStreet") else (addr.get("street") or "")
                            name = lst.get("title") or street or city
                            location = ", ".join(x for x in (street, city, addr.get("state")) if x)

                            ptype = (lst.get("propertyType") or "").lower()
                            results.append({
                                "url":       full_url,
                                "title":     name,
                                "price":     f"${price_int:,}",
                                "price_int": price_int,
                                "location":  location,
                                "date":      "",
                                "source":    "hotpads",
                                "image":     lst.get("medPhotoUrl") or "",
                                "_photos":   lst.get("medPhotoUrls") or [],
                                "_lat":      geo.get("lat"),
                                "_lon":      geo.get("lon"),
                                "_house":    1 if ptype in _HOTPADS_HOUSE_TYPES else 0,
                                "_laundry":  0,   # not exposed on search cards
                                # `activated` = epoch ms when first listed. Move-in
                                # date isn't exposed by the search API and the pad
                                # detail pages are bot-walled, so it stays blank.
                                "_listed_on": epoch_ms_to_ymd(lst.get("activated")),
                                "_avail":    "",
                            })
                        except Exception:
                            continue

                offset += limit
                if offset >= (data.get("numListingsAvailable") or 0):
                    break
                await page.wait_for_timeout(800)
            print(f"         → {len(results)} listings collected (pre-filter)")
        except Exception as e:
            print(f"  [hotpads] Failed: {e}")
        finally:
            await ctx.close()

    # dedupe + area/shared/budget filters
    seen, unique = set(), []
    for r in results:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        if not in_camb_som(r.get("location") or ""):
            continue
        if is_shared(r.get("title") or ""):
            continue
        if r["price_int"] > 2800:
            continue
        unique.append(r)
    return unique


# ── Facebook Marketplace scraper ──────────────────────────────────────────────

# street name token allows numbered streets ("5th", "1st") as well as words
_STREET_RE = re.compile(
    r'\b(\d{1,5}[A-Za-z]?\s+(?:\d{1,3}(?:st|nd|rd|th)|[A-Z][A-Za-z.]*)(?:\s+[A-Za-z.]+){0,3}\s+'
    r'(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Blvd|Boulevard|Ct|Court|'
    r'Pl|Place|Ter|Terrace|Way|Sq|Square|Pkwy|Hwy|Cir|Circle|Row))\b\.?',
    re.I)


def extract_address(text):
    """First street address found in free text (e.g. '111 Sciarappa St'), or ''."""
    m = _STREET_RE.search(text or "")
    return m.group(1).strip().rstrip(".") if m else ""


async def _scrape_fb_pw():
    """Scrape Facebook Marketplace using a PERSISTENT browser profile.

    First run: a real Chrome window opens; log into Facebook once. The session
    is saved under FB_PROFILE_DIR, so subsequent runs reuse it without a login
    prompt. Must run headed (FB blocks headless + you may hit a login/checkpoint).
    """
    from playwright.async_api import async_playwright

    results = []
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            FB_PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Detect login state
        await page.goto("https://www.facebook.com/marketplace/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        needs_login = ("login" in page.url.lower()) or bool(await page.query_selector('input[name="email"]'))
        if needs_login:
            print("  [FB] Not logged in. A browser window is open — log into Facebook,")
            print("  [FB] then return here and press Enter to continue...")
            try:
                input()
            except EOFError:
                print("  [FB] No interactive console; cannot complete login. Aborting FB scrape.")
                await ctx.close()
                return []

        print("  [FB] Loading marketplace listings...")
        # NOTE: facebook.com never reaches networkidle (constant background
        # requests) — wait for the listing links themselves instead.
        await page.goto(FB_SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_selector('a[href*="/marketplace/item/"]', timeout=20000)
        except Exception:
            pass  # may legitimately be 0 results; the loop below handles it
        await page.wait_for_timeout(2000)
        await _lazy_scroll(page, steps=10, pause=600)

        items = await page.query_selector_all('a[href*="/marketplace/item/"]')
        seen = set()
        for item in items:
            try:
                href = await item.get_attribute("href")
                if not href:
                    continue
                url = ("https://www.facebook.com" + href if href.startswith("/") else href).split("?")[0]
                if url in seen:
                    continue
                seen.add(url)

                text  = (await item.inner_text()).strip()
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

                # logged-in card lines: [price, title, location]
                price_raw = next((ln for ln in lines if "$" in ln), "")
                non_price = [ln for ln in lines if "$" not in ln]
                title     = non_price[0] if non_price else (lines[0] if lines else "")
                location  = non_price[-1] if len(non_price) > 1 else ""

                # FB ignores the bedrooms URL param — drop studios and 2+ BRs
                if re.search(r'\bstudio\b', title, re.IGNORECASE):
                    continue
                if re.search(r'\b([2-9])\s*(bed|br\b|室)', title, re.IGNORECASE):
                    continue

                price_m   = re.search(r'\$(\d[\d,]+)', price_raw)
                price_int = int(price_m.group(1).replace(",", "")) if price_m else None

                if is_shared(text):
                    continue  # room in a shared unit — hard constraint

                image = await _img_url(item)

                results.append({
                    "url":       url,
                    "title":     title,
                    "price":     price_raw,
                    "price_int": price_int,
                    "location":  location,
                    "date":      "",
                    "source":    "facebook",
                    "image":     image or "",
                    "_laundry":  laundry_score(text),
                    "_avail":    "9/1" if sept_score(text) else "",
                })
            except Exception:
                continue

        # The card only shows the city; open each post and pull the street
        # address from its description (improves geocoding + the area filter).
        if results:
            print(f"  [FB] reading {len(results)} post(s) for street addresses...")
            got = 0
            for r in results:
                try:
                    await page.goto(r["url"], wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(1200)
                    body = await page.inner_text("body")
                    addr = extract_address(body)
                    if addr:
                        city = (r.get("location") or "").strip()
                        r["location"] = (f"{addr}, {city}"
                                         if city and addr.lower() not in city.lower()
                                         else addr)
                        got += 1
                except Exception:
                    continue
            print(f"  [FB] found street addresses for {got}/{len(results)}")

        await ctx.close()
        total = len(results)
        # The FB search is Boston-wide — keep Cambridge/Somerville listings.
        # Cards with no parsed location are kept (can't rule them out).
        results = [r for r in results
                   if not (r.get("location") or "").strip()
                   or in_camb_som((r.get("location") or "") + " " + (r.get("title") or ""))]
        print(f"  [FB] Found {total} listings, kept {len(results)} (Cambridge/Somerville or unlabeled).")
        return results


# ── Commute times (to Broad Institute / Kendall Sq) ──────────────────────────

import time as _time
from math import radians, sin, cos, asin, sqrt

BROAD = (42.36266, -71.08644)  # 415 Main St, Cambridge (Broad Institute)

# Neighborhood/city centroids for listings without a street address
_CENTROIDS = [
    ("east cambridge",  42.3700, -71.0810),
    ("e. cambridge",    42.3700, -71.0810),
    ("e cambridge",     42.3700, -71.0810),
    ("lechmere",        42.3711, -71.0766),
    ("central square",  42.3655, -71.1035),
    ("inman square",    42.3735, -71.0995),
    ("union square",    42.3795, -71.0945),
    ("cambridgeport",   42.3580, -71.1090),
    ("porter square",   42.3884, -71.1191),
    ("davis square",    42.3967, -71.1224),
    ("harvard square",  42.3736, -71.1190),
    ("winter hill",     42.3920, -71.0980),
    ("magoun square",   42.3935, -71.1070),
    ("ball square",     42.3990, -71.1110),
    ("kendall",         42.3625, -71.0862),
    ("porter",          42.3884, -71.1191),
    ("davis",           42.3967, -71.1224),
    ("harvard",         42.3736, -71.1190),
    ("inman",           42.3735, -71.0995),
    ("somerville",      42.3876, -71.0995),
    ("cambridge",       42.3736, -71.1097),
]

# T stations: (name, lat, lon, ride minutes to Kendall incl. transfer/walk at end)
_STATIONS = [
    ("Kendall/MIT",     42.36249, -71.08617,  0),
    ("Central",         42.36541, -71.10366,  4),
    ("Harvard",         42.37352, -71.11892,  7),
    ("Porter",          42.38843, -71.11912,  9),
    ("Davis",           42.39674, -71.12212, 11),
    ("Alewife",         42.39616, -71.14176, 13),
    ("Lechmere",        42.37116, -71.07655, 15),   # GLX + ~13 min walk to Broad
    ("Union Sq",        42.37745, -71.09480, 20),
    ("East Somerville", 42.37902, -71.08643, 22),
    ("Gilman Sq",       42.38744, -71.09653, 24),
    ("Magoun Sq",       42.39350, -71.10617, 26),
    ("Ball Sq",         42.39992, -71.11135, 28),
]

_WALK_M_PER_MIN = 80    # 4.8 km/h
_BIKE_M_PER_MIN = 233   # 14 km/h
_ROUTE_FACTOR   = 1.3   # streets aren't straight lines
_BUS_SLOWDOWN   = 1.7   # bus in-vehicle vs driving: frequent stops, dwell, no express


def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


def _geocode_nominatim(query):
    """Free OSM geocoder. 1 req/s politeness limit; caller must sleep."""
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1, "countrycodes": "us"})
    req = urllib.request.Request(url, headers={"User-Agent": "aptsearch-tracker/1.0 (personal apartment search)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


def _centroid_for(text):
    if not text:
        return None
    t = text.lower()
    for name, lat, lon in _CENTROIDS:
        if name in t:
            return lat, lon
    return None


def resolve_coords(r):
    """(lat, lon, precision) for a listing row/dict. precision: addr|hood|city.
    Street addresses get geocoded; otherwise neighborhood/city centroids."""
    title    = (r["title"] or "") if not isinstance(r, dict) else (r.get("title") or "")
    location = (r["location"] or "") if not isinstance(r, dict) else (r.get("location") or "")
    url      = (r["url"] or "") if not isinstance(r, dict) else (r.get("url") or "")

    # address-like location ("57 Cedar St, Cambridge, MA 02140") → real geocode
    if re.search(r'\d+\s+\w+.*\b(st|street|ave|avenue|rd|road|pl|place|ct|court|sq|way|dr|drive|ter|terrace)\b',
                 location, re.IGNORECASE):
        pt = _geocode_nominatim(location)
        _time.sleep(1.05)  # Nominatim rate limit
        if pt:
            return pt[0], pt[1], "addr"

    # neighborhood keyword in location or title
    pt = _centroid_for(location) or _centroid_for(title)
    if pt:
        return pt[0], pt[1], "hood"

    # city from CL URL slug or text
    if "/d/somerville" in url or "somerville" in (location + title).lower():
        return 42.3876, -71.0995, "city"
    return 42.3736, -71.1097, "city"  # Cambridge default


def _osrm_minutes(profile, lat, lon):
    """Real route duration via the free OSM routing instances. None on failure."""
    url = (f"https://routing.openstreetmap.de/routed-{profile}/route/v1/driving/"
           f"{lon},{lat};{BROAD[1]},{BROAD[0]}?overview=false")
    req = urllib.request.Request(url, headers={"User-Agent": "aptsearch-tracker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        if data.get("routes"):
            return int(round(data["routes"][0]["duration"] / 60))
    except Exception:
        pass
    return None


def _osrm_route_geo(profile, lat, lon):
    """Simplified [[lat,lon],...] geometry of the route to Broad. None on failure.
    Retries a couple times — the free instance intermittently refuses TLS under load."""
    url = (f"https://routing.openstreetmap.de/routed-{profile}/route/v1/driving/"
           f"{lon},{lat};{BROAD[1]},{BROAD[0]}?overview=simplified&geometries=geojson")
    req = urllib.request.Request(url, headers={"User-Agent": "aptsearch-tracker/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = _json.loads(resp.read().decode())
            if data.get("routes"):
                coords = data["routes"][0]["geometry"]["coordinates"]
                return [[round(c[1], 5), round(c[0], 5)] for c in coords]
            return None
        except Exception:
            _time.sleep(1.5 * (attempt + 1))
    return None


_MBTA_BASE = "https://api-v3.mbta.com"
_MBTA_KEY = os.environ.get("MBTA_API_KEY", "")  # optional free key → higher rate limit
_BROAD_BUS_ROUTES = None          # cached: route ids with a bus stop near Broad
_mbta_cache = {}                  # (lat3,lon3) -> (walk_to_stop_min, has_direct) | None


def _mbta_get(path, params):
    if not _MBTA_KEY:
        _time.sleep(1.1)  # keyless limit is 20 req/min — stay polite
    url = f"{_MBTA_BASE}{path}?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "aptsearch-tracker/1.0"}
    if _MBTA_KEY:
        headers["x-api-key"] = _MBTA_KEY
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            return _json.loads(resp.read().decode())
    except Exception:
        return None


def _mbta_bus_routes_near(lat, lon, radius=0.006):
    """(set of bus route ids near point, (nearest_stop_lat, nearest_stop_lon)). (None,None) on failure."""
    stops = _mbta_get("/stops", {
        "filter[latitude]": lat, "filter[longitude]": lon, "filter[radius]": radius,
        "filter[route_type]": 3, "sort": "distance", "page[limit]": 12,
        "fields[stop]": "latitude,longitude",
    })
    if not stops or not stops.get("data"):
        return None, None
    stop_ids = [s["id"] for s in stops["data"]]
    a = stops["data"][0]["attributes"]
    nearest = (a.get("latitude"), a.get("longitude"))
    routes = _mbta_get("/routes", {"filter[stop]": ",".join(stop_ids), "filter[type]": 3})
    rids = {r["id"] for r in routes["data"]} if routes and routes.get("data") else set()
    return rids, nearest


def _kendall_bus_routes():
    """Bus routes serving a stop near the Broad Institute (fetched once, cached)."""
    global _BROAD_BUS_ROUTES
    if _BROAD_BUS_ROUTES is None:
        rids, _ = _mbta_bus_routes_near(BROAD[0], BROAD[1], radius=0.006)
        _BROAD_BUS_ROUTES = rids or set()
    return _BROAD_BUS_ROUTES


def mbta_bus_access(lat, lon):
    """(walk_to_nearest_bus_stop_min, has_one_seat_kendall_bus) from live MBTA data. None on failure."""
    key = (round(lat, 3), round(lon, 3))
    if key in _mbta_cache:
        return _mbta_cache[key]
    rids, nstop = _mbta_bus_routes_near(lat, lon)
    if rids is None:
        _mbta_cache[key] = None
        return None
    kend = _kendall_bus_routes()
    has_direct = bool(rids & kend) if kend else False
    if nstop and nstop[0] is not None:
        walk_to = _haversine_m(lat, lon, nstop[0], nstop[1]) * _ROUTE_FACTOR / _WALK_M_PER_MIN
        stop_ll = (nstop[0], nstop[1])
    else:
        walk_to = 5.0
        stop_ll = None
    result = (walk_to, has_direct, stop_ll)
    _mbta_cache[key] = result
    return result


def compute_commutes(lat, lon):
    """(walk_min, bike_min, transit_min, bus_min) from a point to the Broad Institute."""
    dist = _haversine_m(lat, lon, *BROAD)

    walk = _osrm_minutes("foot", lat, lon)
    if walk is None:
        walk = int(round(dist * _ROUTE_FACTOR / _WALK_M_PER_MIN))

    bike = _osrm_minutes("bike", lat, lon)
    if bike is None:
        bike = int(round(dist * _ROUTE_FACTOR / _BIKE_M_PER_MIN))

    # transit: walk to best station + 4 min wait + ride to Kendall (+3 min walk out)
    best = None
    for _name, slat, slon, ride in _STATIONS:
        walk_to = _haversine_m(lat, lon, slat, slon) * _ROUTE_FACTOR / _WALK_M_PER_MIN
        total = int(round(walk_to + 4 + ride + 3))
        if best is None or total < best:
            best = total
    transit = min(best, walk) if best is not None else walk  # never worse than walking

    # bus: driving route as the in-vehicle proxy (slowed for stops/dwell), plus
    # real walk-to-stop and a transfer penalty when no one-seat Kendall bus exists.
    # Live MBTA stop/route data; falls back to a flat-access estimate if API is down.
    car = _osrm_minutes("car", lat, lon)
    if car is None:
        car = int(round(dist * _ROUTE_FACTOR / 350))  # ~21 km/h fallback
    access = mbta_bus_access(lat, lon)
    if access is not None:
        walk_to_stop, has_direct = access[0], access[1]
        transfer = 0 if has_direct else 8   # no one-seat Kendall bus → add a transfer
        bus = int(round(walk_to_stop + 6 + car * _BUS_SLOWDOWN + 2 + transfer))
    else:
        bus = int(round(4 + 6 + car * _BUS_SLOWDOWN + 2))  # estimate-only fallback
    bus = min(bus, walk)  # never worse than just walking

    return walk, bike, transit, bus


def _nearest_station(lat, lon):
    """(lat, lon) of the Kendall-bound station the transit model would pick."""
    best, bestd = None, None
    for _n, slat, slon, ride in _STATIONS:
        walk_to = _haversine_m(lat, lon, slat, slon) * _ROUTE_FACTOR / _WALK_M_PER_MIN
        total = walk_to + 4 + ride + 3
        if bestd is None or total < bestd:
            bestd, best = total, (slat, slon)
    return best


def compute_routes(lat, lon):
    """Polylines to Broad per mode: real road route for walk/bike; straight
    origin→stop→Broad indicators for subway/bus (approximate, not track geometry)."""
    routes = {}
    o = [round(lat, 5), round(lon, 5)]
    b = [round(BROAD[0], 5), round(BROAD[1], 5)]
    wb = _osrm_route_geo("bike", lat, lon) or _osrm_route_geo("foot", lat, lon)
    routes["walkbike"] = wb if wb else [o, b]  # straight fallback if geometry unavailable
    st = _nearest_station(lat, lon)
    if st:
        routes["subway"] = [o, [round(st[0], 5), round(st[1], 5)], b]
    access = mbta_bus_access(lat, lon)
    stop = access[2] if access else None
    routes["bus"] = [o, [round(stop[0], 5), round(stop[1], 5)], b] if stop else [o, b]
    return routes


# ── Google Maps Directions (real times + real route geometry) ────────────────

_GOOGLE_KEY = None


def _google_key():
    """API key from .google_key next to this script ('' if absent → use OSRM/MBTA)."""
    global _GOOGLE_KEY
    if _GOOGLE_KEY is None:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".google_key")) as f:
                _GOOGLE_KEY = f.read().strip()
        except Exception:
            _GOOGLE_KEY = ""
    return _GOOGLE_KEY


def _decode_polyline(s):
    """Decode a Google encoded polyline → [[lat,lon],...]."""
    pts, idx, lat, lng, n = [], 0, 0, 0, len(s)
    while idx < n:
        shift = result = 0
        while True:
            b = ord(s[idx]) - 63; idx += 1
            result |= (b & 0x1f) << shift; shift += 5
            if b < 0x20:
                break
        lat += ~(result >> 1) if (result & 1) else (result >> 1)
        shift = result = 0
        while True:
            b = ord(s[idx]) - 63; idx += 1
            result |= (b & 0x1f) << shift; shift += 5
            if b < 0x20:
                break
        lng += ~(result >> 1) if (result & 1) else (result >> 1)
        pts.append([round(lat * 1e-5, 5), round(lng * 1e-5, 5)])
    return pts


def _google_directions(mode, lat, lon, transit_mode=None, want_steps=False):
    """(minutes, geometry) to Broad via Google. (None, None) on failure.
    geometry = overview [[lat,lon],...] normally, or per-step
    [{"mode":"walk"|"transit","pts":[...]}] when want_steps (for transit)."""
    key = _google_key()
    if not key:
        return None, None
    params = {"origin": f"{lat},{lon}", "destination": f"{BROAD[0]},{BROAD[1]}",
              "mode": mode, "key": key}
    if transit_mode:
        params["transit_mode"] = transit_mode
    url = "https://maps.googleapis.com/maps/api/directions/json?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "aptsearch/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = _json.loads(resp.read().decode())
        if d.get("status") == "OK" and d.get("routes"):
            route = d["routes"][0]
            leg = route["legs"][0]
            mins = int(round(leg["duration"]["value"] / 60))
            if want_steps:
                segs = []
                for st in leg.get("steps", []):
                    pts = _decode_polyline(st.get("polyline", {}).get("points", ""))
                    if not pts:
                        continue
                    m = "walk" if st.get("travel_mode") == "WALKING" else "transit"
                    segs.append({"mode": m, "pts": pts})
                return mins, (segs or None)
            poly = _decode_polyline(route.get("overview_polyline", {}).get("points", ""))
            return mins, (poly or None)
    except Exception:
        pass
    return None, None


def google_commute(lat, lon):
    """((walk,bike,subway,bus) minutes, {walkbike,subway,bus} polylines) via Google."""
    o = [round(lat, 5), round(lon, 5)]
    b = [round(BROAD[0], 5), round(BROAD[1], 5)]
    walk_m, walk_geo   = _google_directions("walking", lat, lon)
    bike_m, bike_geo   = _google_directions("bicycling", lat, lon)
    sub_m,  sub_segs   = _google_directions("transit", lat, lon, transit_mode="subway", want_steps=True)
    bus_m,  bus_segs   = _google_directions("transit", lat, lon, transit_mode="bus", want_steps=True)
    dist = _haversine_m(lat, lon, *BROAD)
    if walk_m is None:
        walk_m = int(round(dist * _ROUTE_FACTOR / _WALK_M_PER_MIN))
    if bike_m is None:
        bike_m = int(round(dist * _ROUTE_FACTOR / _BIKE_M_PER_MIN))
    if sub_m is None:
        sub_m = walk_m
    if bus_m is None:
        bus_m = walk_m
    routes = {"walk": walk_geo or [o, b], "bike": bike_geo or [o, b]}
    if sub_segs:
        routes["subway"] = sub_segs
    if bus_segs:
        routes["bus"] = bus_segs
    return (walk_m, bike_m, sub_m, bus_m), routes


def compute_missing_commutes(conn, log=print, recompute=False):
    """Fill lat/lon + walk/bike/transit minutes for rows missing them."""
    where = "1=1" if recompute else "walk_min IS NULL OR bus_min IS NULL OR route_geo IS NULL"
    rows = conn.execute(f"SELECT * FROM listings WHERE {where}").fetchall()
    if not rows:
        return 0
    log(f"Computing commutes for {len(rows)} listing(s)...")
    done = 0
    for r in rows:
        try:
            # listings with exact coords from the source (e.g. Zillow) skip geocoding
            try:
                has_coords = bool(r["lat"] and r["lon"])
            except (KeyError, IndexError):
                has_coords = False
            if has_coords:
                lat, lon, src = r["lat"], r["lon"], (r["geo_src"] or "addr")
            else:
                lat, lon, src = resolve_coords(r)
            if _google_key():
                (walk, bike, transit, bus), routes = google_commute(lat, lon)
            else:
                walk, bike, transit, bus = compute_commutes(lat, lon)
                routes = compute_routes(lat, lon)
            conn.execute(
                "UPDATE listings SET lat=?, lon=?, geo_src=?, walk_min=?, bike_min=?, transit_min=?, bus_min=?, route_geo=? WHERE id=?",
                (lat, lon, src, walk, bike, transit, bus, _json.dumps(routes), r["id"]),
            )
            conn.commit()
            done += 1
        except Exception as e:
            log(f"  #{r['id']} failed: {e}")
    log(f"Commutes computed for {done}/{len(rows)}.")
    return done


def backfill_sv_headings(conn, log=print):
    """Store the building-facing Street View heading per listing (free metadata
    calls), so the grid can animate a pan without a lookup per render."""
    if not _google_key():
        return 0
    rows = conn.execute(
        "SELECT id, lat, lon FROM listings WHERE lat IS NOT NULL "
        "AND sv_heading IS NULL AND COALESCE(delisted,0)=0").fetchall()
    n = 0
    for r in rows:
        h = streetview_heading(r["lat"], r["lon"])
        if h is not None:
            conn.execute("UPDATE listings SET sv_heading=? WHERE id=?", (h, r["id"]))
            n += 1
    if n:
        conn.commit()
    log(f"  street-view headings filled: {n}")
    return n


def row_commute_html(r):
    """'🚶 18m · 🚲 9m · 🚇 14m · 🚌 22m' chip; ~ prefix when location was approximate."""
    try:
        w, b, t = r["walk_min"], r["bike_min"], r["transit_min"]
        src = r["geo_src"] or ""
    except (KeyError, IndexError):
        return ""
    if w is None:
        return ""
    try:
        bus = r["bus_min"]
    except (KeyError, IndexError):
        bus = None
    approx = "~" if src != "addr" else ""
    bus_html = f' &nbsp;&#128652;{approx}{bus}m' if bus is not None else ""
    return (f'<span class="commute">&#128694;{approx}{w}m &nbsp;&#128692;{approx}{b}m '
            f'&nbsp;&#128647;{approx}{t}m{bus_html}</span>')


def row_commute_col(r):
    """Vertical commute column (🚶 walk · 🚴 bike · 🚇 subway · 🚌 bus), one per row."""
    try:
        w, b, t = r["walk_min"], r["bike_min"], r["transit_min"]
        src = r["geo_src"] or ""
    except (KeyError, IndexError):
        return ""
    if w is None:
        return ""
    try:
        bus = r["bus_min"]
    except (KeyError, IndexError):
        bus = None
    approx = "~" if src != "addr" else ""
    items = [("&#128694;", w, "cm-walk"), ("&#128692;", b, "cm-bike")]   # 🚶 / 🚴 — green
    # only the faster public-transit option (subway vs bus), in purple — and only
    # when it differs from the walk time
    cands = [(x, e) for x, e in ((t, "&#128647;"), (bus, "&#128652;")) if x is not None]
    if cands:
        tm, te = min(cands, key=lambda x: x[0])
        if tm != w:
            items.append((te, tm, "cm-transit"))                        # 🚇/🚌 — purple
    rows = "".join(
        f'<div class="cm {cls}"><span class="cm-ico">{ic}</span>{approx}{m}m</div>'
        for ic, m, cls in items
    )
    return f'<div class="commutes">{rows}</div>'


def cmd_commute(args):
    conn = db_connect()
    compute_missing_commutes(conn, recompute=args.all)


# ── East Cambridge priority (user's #1 neighborhood — always shown first) ────

_EC_CENTER = (42.3700, -71.0810)

def row_is_east_cam(r):
    """East Cambridge: explicit mention, 02141/02142 zip, or within ~0.9km of EC center."""
    text = ((r["title"] or "") + " " + (r["location"] or "")).lower()
    if "east cambridge" in text or re.search(r'\b0214[12]\b', text):
        return 1
    try:
        if r["lat"] and r["lon"] and _haversine_m(r["lat"], r["lon"], *_EC_CENTER) < 900:
            return 1
    except (KeyError, IndexError):
        pass
    return 0


# ── Derived metadata: neighborhood · unit type · amenities ───────────────────

_HOOD_POINTS = [
    ("East Cambridge", 42.3700, -71.0810),
    ("Kendall Square", 42.3625, -71.0862),
    ("Inman Square",   42.3735, -71.0995),
    ("Central Square", 42.3655, -71.1035),
    ("Cambridgeport",  42.3580, -71.1090),
    ("Harvard Square", 42.3736, -71.1190),
    ("Porter Square",  42.3884, -71.1191),
    ("Davis Square",   42.3967, -71.1224),
    ("Union Square",   42.3795, -71.0945),
    ("Spring Hill",    42.3855, -71.1010),
    ("Winter Hill",    42.3920, -71.0980),
    ("Magoun Square",  42.3935, -71.1070),
    ("Ball Square",    42.3990, -71.1110),
    ("Teele Square",   42.4030, -71.1230),
]


def classify_neighborhood(r):
    """Human neighborhood label (East Cambridge prioritized, else nearest centroid)."""
    if row_is_east_cam(r):
        return "East Cambridge"
    try:
        lat, lon = r["lat"], r["lon"]
    except (KeyError, IndexError):
        lat = lon = None
    if lat and lon:
        best, bestd = None, 1e18
        for name, clat, clon in _HOOD_POINTS:
            d = _haversine_m(lat, lon, clat, clon)
            if d < bestd:
                bestd, best = d, name
        if best:
            return best
    return (r["location"] or "").strip() or "—"


def row_unit_type(r):
    if is_shared((r["title"] or "") + " " + (r["location"] or "")):
        return "room"
    return "house" if row_is_house(r) else "apt"


_UTYPE_EMOJI = {"house": "🏠", "apt": "🏢", "room": "🚪"}


# Amenity detection from the (limited) listing text we capture — title + location.
_PARK_YES = re.compile(r'\b(off.?street\s+parking|driveway|garage|deeded\s+parking|'
                       r'parking\s+(?:incl\w*|space|spot|available|included)|street\s+parking)\b', re.I)
_PARK_NO  = re.compile(r'\bno\s+parking\b', re.I)
_DISH_YES = re.compile(r'\b(dishwasher|dish\s?washer|d/?w)\b', re.I)
_DISH_NO  = re.compile(r'\bno\s+dishwasher\b', re.I)
_DISP_YES = re.compile(r'\b(garbage\s+disposal|insinkerator|disposal)\b', re.I)
_WIFI_YES = re.compile(r'\b(wi-?fi|wireless\s+internet|internet\s+included|high.?speed\s+internet)\b', re.I)
_HEAT_YES = re.compile(r'\b(heat(?:ing)?(?:\s+(?:incl\w*|included))?|hot\s*water\s+incl\w*|'
                       r'heat\s*[&/]\s*hot\s*water|radiator|forced\s+(?:hot\s+)?air|central\s+heat)\b', re.I)


def _amen_flag(text, yes_re, no_re=None):
    if no_re and no_re.search(text):
        return "no"
    if yes_re.search(text):
        return "yes"
    return "unknown"


def row_amenities(r):
    """{laundry, parking, dishwasher, disposal, wifi, heat} each yes|no|unknown.
    Scans title + location + amen_text (the rich amenities list scraped from
    detail pages, when available)."""
    def _g(k):
        try:
            return r[k] or ""
        except (KeyError, IndexError):
            return ""
    amen = _g("amen_text")
    text = " ".join([r["title"] or "", r["location"] or "", amen])
    laundry = "yes" if (row_has_laundry(r) or _LAUNDRY_RE.search(amen)) else "unknown"
    return {
        "laundry":    laundry,
        "parking":    _amen_flag(text, _PARK_YES, _PARK_NO),
        "dishwasher": _amen_flag(text, _DISH_YES, _DISH_NO),
        "disposal":   _amen_flag(text, _DISP_YES),
        "wifi":       _amen_flag(text, _WIFI_YES),
        "heat":       _amen_flag(text, _HEAT_YES),
    }


def row_meta(r):
    """Dict-type metadata stored in the `meta` column and shown on cards."""
    return {
        "neighborhood": classify_neighborhood(r),
        "source": r["source"] or "",
        "unit_type": row_unit_type(r),
    }


def backfill_derived(conn):
    """Populate neighborhood / meta / amenities columns for all rows.

    Derived values are deterministic, so only rows that actually changed are
    written — repeated calls (e.g. on every page load) issue no UPDATEs when
    nothing changed, which matters for the remote/shared DB where each write is
    a network round-trip."""
    dirty = False
    for r in conn.execute("SELECT * FROM listings").fetchall():
        meta = row_meta(r)
        hood = meta["neighborhood"]
        meta_json = _json.dumps(meta)
        amen_json = _json.dumps(row_amenities(r))
        if r["neighborhood"] == hood and r["meta"] == meta_json and r["amenities"] == amen_json:
            continue
        conn.execute(
            "UPDATE listings SET neighborhood=?, meta=?, amenities=? WHERE id=?",
            (hood, meta_json, amen_json, r["id"]),
        )
        dirty = True
    if dirty:
        conn.commit()


_AM_DEFS = [
    ("laundry",    "&#129530;", "In-unit laundry"),    # 🧺
    ("parking",    "&#128663;", "Parking"),            # 🚗
    ("dishwasher", "&#127869;", "Dishwasher"),         # 🍽
    ("disposal",   "&#128688;", "Garbage disposal"),   # 🚰
    ("wifi",       "&#128246;", "WiFi / internet"),    # 📶
    ("heat",       "&#128293;", "Heat"),               # 🔥
]
_AM_MARK = {"yes": "&#10003;", "no": "&#10007;", "unknown": "?"}   # ✓ ✗ ?
_AM_CLS  = {"yes": "am-yes", "no": "am-no", "unknown": "am-unk"}


def amenities_html(am):
    rows = []
    for key, icon, label in _AM_DEFS:
        v = am.get(key, "unknown")
        rows.append(
            f'<div class="am {_AM_CLS[v]}" title="{label}: {v}">'
            f'<span class="am-ico">{icon}</span><b>{_AM_MARK[v]}</b></div>'
        )
    return f'<div class="amenities">{"".join(rows)}</div>'


# Display cutoff: hide listings farther than this by bike (user constraint).
MAX_BIKE_MIN = 15


def _row_bike(r):
    try:
        b = r["bike_min"]
        return b if b is not None else 998  # unknown sorts last but is shown
    except (KeyError, IndexError):
        return 998


def row_rating(r):
    """User rating ('no'/'mid'/'nice') or '' — safe across sqlite3.Row and _Row."""
    try:
        return r["rating"] or ""
    except (KeyError, IndexError):
        return ""


def _row_get(r, k):
    try:
        return r[k]
    except (KeyError, IndexError):
        return None


def row_specs_html(r):
    """Colored spec spans: '1bd 1ba' (no dot) in one color + sqft in another."""
    beds, baths, sqft = _row_get(r, "beds"), _row_get(r, "baths"), _row_get(r, "sqft")
    out = ""
    bb = []
    if beds is not None:
        bb.append(f"{beds:g}bd")
    if baths is not None:
        bb.append(f"{baths:g}ba")
    if bb:
        out += f'<span class="spec-bba">{" ".join(bb)}</span>'
    if sqft:
        out += f'<span class="spec-sqft">{sqft:,} ft&sup2;</span>'
    return out


def row_photos(r):
    """List of photo URLs for a listing — parsed from the photos JSON column,
    falling back to the single thumbnail image."""
    raw = _row_get(r, "photos")
    if raw:
        try:
            pics = _json.loads(raw)
            if isinstance(pics, list) and pics:
                return pics
        except (ValueError, TypeError):
            pass
    img = _row_get(r, "image")
    return [img] if img else []


_UNIT_RE = re.compile(r'(?:\b(?:unit|apt|apartment)\.?\s*#?\s*|#)\s*([0-9]+[A-Za-z]?|[A-Za-z][0-9]*)\b', re.I)


def row_unit(r):
    """Unit number pulled from the title (e.g. 'Unit 2R', '#3'), or '' if none."""
    m = _UNIT_RE.search(r["title"] or "")
    return m.group(1) if m else ""


def _row_walk(r):
    try:
        w = r["walk_min"]
        return w if w is not None else 998
    except (KeyError, IndexError):
        return 998


def _row_date_key(r):
    """Listing date (listed_on, else first-seen added_on) as a YYYYMMDD int,
    negated so the newest sorts first; 0 (no date) sorts last."""
    try:
        d = r["listed_on"]
    except (KeyError, IndexError):
        d = None
    if not d:
        try:
            d = r["added_on"]
        except (KeyError, IndexError):
            d = None
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', d or "")
    return -int(m.group(1) + m.group(2) + m.group(3)) if m else 0


def _row_sort_key(r):
    """Within a status section: newest listing date first, then shortest walk
    time, then a few stable tiebreakers (East Cambridge, bike time, id)."""
    return (
        _row_date_key(r),     # newest listed/first-seen first
        _row_walk(r),         # shortest walk to Broad first
        -row_is_east_cam(r),
        _row_bike(r),
        r["id"],
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def neighborhood_score(location):
    if not location:
        return 0
    loc = location.lower()
    for name, score in _NEIGHBORHOOD_MAP.items():
        if name in loc:
            return score
    return 0


def score_stars(score):
    return ("★" * score + "☆" * (5 - score)) if score else "     "


def fmt_price(price):
    return f"${price:,}" if price else "  —  "


def color(status, text):
    return STATUS_COLORS.get(status, "") + text + RESET


# ── HTML export ────────────────────────────────────────────────────────────────

_SECTION_DEFS = [
    ("applied",    "Applied"),
    ("new",        "New"),
    ("viewed",     "Viewed"),
    ("passed",     "Passed"),
    ("gotaway",    "Got away"),
]


def _row_image(r):
    try:
        return r["image"] or ""
    except (KeyError, IndexError):
        return ""


def _streetview_url(r, size="400x220"):
    """Google Street View Static image URL for a listing, or '' if no key/location.
    Uses exact lat/lon when available, else the address text. return_error_code
    makes locations with no imagery 404 (we hide those client-side) instead of
    serving Google's gray 'no imagery' placeholder."""
    key = _google_key()
    if not key:
        return ""
    if r["lat"] is not None and r["lon"] is not None:
        loc = f'{r["lat"]},{r["lon"]}'
    else:
        loc = (r["location"] or "").strip()
    if not loc:
        return ""
    params = urllib.parse.urlencode({
        "size": size, "location": loc, "fov": 80, "source": "outdoor",
        "return_error_code": "true", "key": key,
    })
    return "https://maps.googleapis.com/maps/api/streetview?" + params


def _streetview_map_link(r):
    """A google.com/maps link to the interactive Street View for the location."""
    if r["lat"] is not None and r["lon"] is not None:
        q = f'{r["lat"]},{r["lon"]}'
    else:
        q = (r["location"] or "").strip()
    return "https://www.google.com/maps?q=&layer=c&cbll=" + urllib.parse.quote(q) if q else ""


def streetview_heading(lat, lon):
    """Compass heading that points the Street View camera AT the building: the
    bearing from the nearest pano (via the metadata endpoint) to lat/lon. Returns
    None if no key / no imagery / lookup fails."""
    key = _google_key()
    if not key or lat is None or lon is None:
        return None
    try:
        url = ("https://maps.googleapis.com/maps/api/streetview/metadata?"
               + urllib.parse.urlencode({"location": f"{lat},{lon}",
                                         "source": "outdoor", "key": key}))
        with urllib.request.urlopen(url, timeout=8) as resp:
            meta = _json.loads(resp.read().decode("utf-8", "replace"))
        if meta.get("status") != "OK":
            return None
        loc = meta.get("location") or {}
        plat, plon = loc.get("lat"), loc.get("lng")
        if plat is None or plon is None:
            return None
        from math import radians, degrees, sin, cos, atan2
        dlon = radians(lon - plon)  # bearing from pano toward the listing
        y = sin(dlon) * cos(radians(lat))
        x = (cos(radians(plat)) * sin(radians(lat))
             - sin(radians(plat)) * cos(radians(lat)) * cos(dlon))
        return round((degrees(atan2(y, x)) + 360) % 360)
    except Exception:
        return None


def _render_card(r, is_new_today=False, interactive=False, units=None):
    status     = r["status"]
    sc         = neighborhood_score(r["location"] or "")
    stars_str  = "★" * sc + "☆" * (5 - sc) if sc else "—"
    # multi-unit building: headline a price range across the available units
    if units and len(units) > 1:
        prices = sorted(u["price"] for u in units if u["price"])
        if prices and prices[0] != prices[-1]:
            price_str = f"${prices[0]:,}–${prices[-1]:,}"
        elif prices:
            price_str = f"${prices[0]:,}"
        else:
            price_str = "—"
    else:
        price_str = f"${r['price']:,}" if r["price"] else "—"
    src        = (r["source"] or "").lower()
    src_badge  = f'<span class="src src-{src}">{src}</span>' if src else ""
    is_house   = row_is_house(r)
    house_badge = '<span class="house-badge">house unit</span>' if is_house else ""
    house_cls  = " is-house" if is_house else ""
    laundry_badge = '<span class="laundry-badge">in-unit laundry</span>' if row_has_laundry(r) else ""
    is_ec      = row_is_east_cam(r)
    ec_badge   = ""  # East Cambridge tag removed per request (still sorted first)
    ec_cls     = " east-cam" if is_ec else ""
    avail      = row_available(r)
    avail_html = f'<span class="avail">avail {avail}</span>' if avail else ""
    commute_col = row_commute_col(r)
    if r["lat"] is not None and r["lon"] is not None:
        approx = 0 if (r["geo_src"] or "") == "addr" else 1
        try:
            rg = r["route_geo"] or ""
        except (KeyError, IndexError):
            rg = ""
        routes_attr = f" data-routes='{rg}'" if rg else ""
        def _g(k):
            try:
                return r[k]
            except (KeyError, IndexError):
                return None
        walk_min = _g("walk_min")
        # faster public-transit option (subway vs bus) → purple line on the map
        cands = [(t, m) for t, m in
                 ((_g("transit_min"), "subway"), (_g("bus_min"), "bus")) if t is not None]
        transit = min(cands, key=lambda x: x[0]) if cands else None
        show_transit = bool(transit and walk_min is not None and transit[0] != walk_min)
        transit_attr = f' data-tmode="{transit[1]}"' if show_transit else ""
        minimap_html = (
            f'<div class="minimap-wrap">'
            f'<div class="minimap" data-lat="{r["lat"]}" data-lon="{r["lon"]}" '
            f'data-approx="{approx}"{transit_attr}{routes_attr}></div>'
            f'</div>'
        )
    else:
        minimap_html = ""
    hood       = classify_neighborhood(r)
    unit_type  = row_unit_type(r)
    hood_html  = ""  # neighborhood + unit type now live on the price line
    amen_html  = amenities_html(row_amenities(r))
    commute_block = (f'<div class="ac-col"><div class="ac-h">Commute</div>{commute_col}</div>'
                     if commute_col else "")
    addr       = (r["location"] or "").strip()
    addr       = re.sub(r'\s*\b\d{5}(?:-\d{4})?\b\s*$', '', addr).strip().rstrip(",").strip()
    addr       = re.sub(r',?\s*\b(MA|Mass|Massachusetts)\b\s*$', '', addr, flags=re.I).strip().rstrip(",").strip()
    # include the unit number (from the title) in the address when not already there
    unit = row_unit(r)
    if unit and not re.search(r'(?:unit|apt|#)\s*' + re.escape(unit) + r'\b', addr, re.I):
        addr = f"{addr} #{unit}" if addr else f"#{unit}"
    avail_pl = f'<span class="pl-avail">{avail}</span>' if avail else ""
    price_line = (
        f'<div class="price-line">'
        f'<a href="{r["url"]}" target="_blank">{price_str}<span class="permo">/mo</span></a>'
        f'<span class="utype utype-{unit_type} pl-utype" title="{unit_type}">{_UTYPE_EMOJI.get(unit_type, unit_type)}</span>'
        f'{avail_pl}</div>'
    )
    specs_html = row_specs_html(r)
    spec_line  = f'<div class="spec-line">{specs_html}</div>' if specs_html else ""
    addr_inner = f'<a href="{r["url"]}" target="_blank">{addr}</a>' if addr else ""
    if hood:
        addr_inner += (f'{" &middot; " if addr else ""}<span class="spec-hood">{hood}</span>')
    addr_line  = f'<div class="addr-line">{addr_inner}</div>' if addr_inner else ""
    # multi-unit building: list each available unit, linking to its own page
    units_html = ""
    if units and len(units) > 1:
        links = "".join(
            f'<a href="{u["url"]}" target="_blank" class="unit-chip">'
            f'{("#" + u["label"]) if u["label"] else "unit"}'
            f'{(" · $" + format(u["price"], ",")) if u["price"] else ""}</a>'
            for u in sorted(units, key=lambda u: (u["price"] or 10**9))
        )
        units_html = (f'<div class="units-avail"><div class="ac-h">{len(units)} units available</div>'
                      f'<div class="units-list">{links}</div></div>')
    addr_html  = ""  # caption strip removed; price/spec/addr now live in card-body
    new_flag   = ' <span class="new-today">NEW TODAY</span>' if is_new_today else ""
    notes_html = (
        f'<div class="notes">{r["notes"]}</div>'
        if r["notes"] and r["notes"].strip() else ""
    )
    img = _row_image(r)
    img_cell = (
        f'<a href="{r["url"]}" target="_blank" class="thumb-link">'
        f'<img class="thumb" src="{img}" loading="lazy" alt="" '
        f'onerror="this.closest(&quot;.card&quot;).classList.add(&quot;no-img&quot;)"></a>'
        if img else ""
    )
    sv_url  = _streetview_url(r)
    sv_link = _streetview_map_link(r)
    sv_base = _row_get(r, "sv_heading")
    sv_base_attr = f' data-svbase="{sv_base}"' if sv_base is not None else ""
    sv_cell = (
        f'<a href="{sv_link}" target="_blank" class="sv-link" title="Open Street View">'
        f'<img class="streetview" src="{sv_url}"{sv_base_attr} loading="lazy" alt="Street View" '
        f'onerror="this.closest(&quot;.sv-link&quot;).style.display=&quot;none&quot;">'
        f'<span class="sv-tag">&#128247; Street View</span></a>'
        if sv_url else ""
    )
    # Street View + minimap share a horizontal split; listing photo sits on top.
    split_html = (
        f'<div class="media-split">{sv_cell}{minimap_html}</div>'
        if (sv_cell or minimap_html) else ""
    )
    media_row = (
        f'<div class="media-row">{img_cell}{split_html}</div>'
        if (img_cell or split_html) else ""
    )
    new_badge = '<span class="banner-new">&#9733; NEW</span>' if is_new_today else ""
    try:
        listed = r["listed_on"]
    except (KeyError, IndexError):
        listed = None
    banner_date = (listed or (r["added_on"] or ""))[:10]
    banner_html = (
        f'<div class="card-banner src-{src}">'
        f'<span class="banner-src">{src or "—"}</span>'
        f'<span class="banner-date">{banner_date}</span>'
        f'{new_badge}</div>'
    )
    laundry_cls = " has-laundry" if row_has_laundry(r) else ""
    try:
        is_delisted = bool(r["delisted"])
    except (KeyError, IndexError):
        is_delisted = False
    delisted_cls = " delisted" if is_delisted else ""
    delisted_banner = '<div class="delisted-banner">&#9888; REMOVED BY AUTHOR</div>' if is_delisted else ""
    is_dup = bool(_row_get(r, "duplicate"))
    dup_cls = " duplicate" if is_dup else ""
    dup_banner = '<div class="dup-banner">&#128203; DUPLICATE ADDRESS</div>' if is_dup else ""
    rating = row_rating(r)
    rating_col = ""
    status_col = ""
    if interactive:
        rid = r["id"]
        def _ron(val):  # mark the currently-selected rating button
            return " rated-on" if rating == val else ""
        # Status buttons as a column to the right of commute.
        status_col = (
            f'<div class="ac-col"><div class="ac-h">Status</div>'
            f'<div class="status-col">'
            f'<button class="act act-viewed" title="viewed" onclick="setStatus({rid},\'viewed\')">👀</button>'
            f'<button class="act act-applied" title="applied" onclick="setStatus({rid},\'applied\')">📝</button>'
            f'<button class="act act-note" title="note" onclick="addNote({rid})">🗒️</button>'
            f'<button class="act act-gotaway" title="got away (hide)" onclick="passHide({rid},\'gotaway\')">👋</button>'
            f'</div></div>'
        )
        # Bottom row of "how much I like" emojis; ❌ rejects + hides immediately.
        rating_col = (
            f'<div class="rating-row">'
            f'<button class="rate rate-pass" title="pass / hide" onclick="passHide({rid},\'passed\')">😡</button>'
            f'<button class="rate rate-hmm{_ron("hmm")}" title="maybe" onclick="setRating({rid},\'hmm\')">🤔</button>'
            f'<button class="rate rate-ok{_ron("ok")}" title="yes" onclick="setRating({rid},\'ok\')">😊</button>'
            f'<button class="rate rate-love{_ron("love")}" title="love" onclick="setRating({rid},\'love\')">😍</button>'
            f'</div>'
        )
    amen = row_amenities(r)
    amen_commute_html = (
        f'<div class="amen-commute">'
        f'<div class="ac-col"><div class="ac-h">Amenities</div>{amen_html}</div>'
        f'{commute_block}{status_col}</div>'
    )
    amen_yes = " ".join(k for k, v in amen.items() if v == "yes")
    data_attrs = (
        f'data-id="{r["id"]}" data-bike="{_row_bike(r)}" data-source="{src}" '
        f'data-rating="{rating}" data-delisted="{1 if is_delisted else 0}" '
        f'data-price="{r["price"] or 0}" '
        f'data-beds="{_row_get(r, "beds") if _row_get(r, "beds") is not None else ""}" '
        f'data-baths="{_row_get(r, "baths") if _row_get(r, "baths") is not None else ""}" '
        f'data-sqft="{_row_get(r, "sqft") or ""}" '
        f'data-amen="{amen_yes}" data-dup="{1 if is_dup else 0}" '
        f'data-movein="{movein_iso(avail)}" data-added="{banner_date}"'
    )
    return (
        f'<div class="card {status}{house_cls}{laundry_cls}{ec_cls}{delisted_cls}{dup_cls}" {data_attrs}>'
        f'{delisted_banner}'
        f'{dup_banner}'
        f'{banner_html}'
        f'{media_row}'
        f'<div class="card-body">'
        f'{price_line}'
        f'{spec_line}'
        f'{addr_line}'
        f'{units_html}'
        f'{hood_html}'
        f'{amen_commute_html}'
        f'{notes_html}'
        f'{rating_col}'
        f'</div>'
        f'</div>'
    )


def _render_sections(all_rows, new_ids=None, interactive=False):
    new_ids = new_ids or set()
    sections_html = ""
    hidden_far = 0
    for status, label in _SECTION_DEFS:
        # drop cross-source duplicates here; distinct units are merged below
        rows = [r for r in all_rows if r["status"] == status and not _row_get(r, "duplicate")]
        if not rows:
            continue
        if not interactive:
            # static export: hard-hide places beyond the bike cutoff
            # (unknown commute = shown; East Cambridge always shown — it's the
            # top priority and within range by definition; centroid noise shouldn't hide it)
            keep = [r for r in rows
                    if row_is_east_cam(r) or _row_bike(r) <= MAX_BIKE_MIN or _row_bike(r) == 998]
            hidden_far += len(rows) - len(keep)
            rows = keep
            if not rows:
                continue
        rows.sort(key=_row_sort_key)  # East Cambridge first, then distance
        # group units at the same building address into one card
        groups, order = {}, []
        for r in rows:
            key = _norm_building(r) or f"__{r['id']}"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(r)
        card_list = []
        for key in order:
            g = groups[key]
            if len(g) == 1:
                card_list.append(_render_card(g[0], is_new_today=(g[0]["id"] in new_ids),
                                              interactive=interactive))
            else:
                rep = min(g, key=lambda r: (r["price"] or 10**9))  # cheapest = headline
                units = [{"label": row_unit(r), "url": r["url"], "price": r["price"]} for r in g]
                is_new = any(r["id"] in new_ids for r in g)
                card_list.append(_render_card(rep, is_new_today=is_new,
                                              interactive=interactive, units=units))
        cards = "".join(card_list)
        sections_html += (
            f'<div class="section">'
            f'<div class="sec-header">'
            f'<span class="sec-title">{label}</span>'
            f'<span class="count">{len(rows)}</span>'
            f'</div>'
            f'<div class="cards">{cards}</div>'
            f'</div>'
        )
    if hidden_far:
        sections_html += (
            f'<p class="empty">{hidden_far} listing(s) hidden: more than '
            f'{MAX_BIKE_MIN} min by bike from the Broad Institute.</p>'
        )
    return sections_html


def cmd_html(args):
    conn = db_connect()
    backfill_derived(conn)  # keep neighborhood/meta/amenities columns current
    all_rows = conn.execute(
        "SELECT * FROM listings ORDER BY "
        "CASE status WHEN 'interested' THEN 0 WHEN 'applied' THEN 1 WHEN 'new' THEN 2 "
        "WHEN 'viewed' THEN 3 WHEN 'passed' THEN 4 END, id"
    ).fetchall()

    if not all_rows:
        sections_html = (
            '<div class="section"><p class="empty">'
            'No listings yet. Use <code>track.py fetch-cl</code>, '
            '<code>track.py fetch-apts</code>, or <code>track.py fetch-fb</code>.'
            '</p></div>'
        )
    else:
        sections_html = _render_sections(all_rows)

    links_html = "\n".join(
        f'<a href="{url}" target="_blank">{name}</a>'
        for name, url in _SEARCH_LINKS
    )

    hood_rows = "".join(
        f'<tr><td><strong>{n["name"]}</strong></td>'
        f'<td>{n["transit"]}</td>'
        f'<td>{n["avg"]}</td>'
        f'<td class="score-stars">{"★" * n["score"] + "☆" * (5 - n["score"])}</td></tr>'
        for n in NEIGHBORHOODS
    )

    date_str = datetime.now().strftime("%B %d, %Y %H:%M")
    html = _HTML.format(
        date=date_str,
        search_summary="",
        sections=sections_html,
        links=links_html,
        neighborhoods=hood_rows,
    )

    with open(HTML_OUT, "w") as f:
        f.write(html)

    print(f"Saved → {HTML_OUT}")
    print(f"Open  → open {HTML_OUT}")


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_add(args):
    conn = db_connect()
    price = int(args.price) if args.price else None
    fp = fingerprint(args.title, args.location, price)
    dup, dup_id = is_duplicate(conn, args.url, fp)
    if dup:
        print(f"Duplicate of #{dup_id} — not added.")
        return
    ts = now()
    cur = conn.execute(
        "INSERT INTO listings (url, title, price, location, status, notes, source, fingerprint, added_on, updated_on) "
        "VALUES (?, ?, ?, ?, 'new', ?, '', ?, ?, ?)",
        (args.url, args.title, price, args.location, args.notes or "", fp, ts, ts),
    )
    conn.commit()
    print(f"Added listing #{cur.lastrowid}: {args.title or args.url[:60]}")


def cmd_list(args):
    conn = db_connect()
    where, params = "", []
    if args.status:
        where, params = "WHERE status = ?", [args.status]

    rows = conn.execute(
        f"SELECT * FROM listings {where} ORDER BY "
        "CASE status WHEN 'interested' THEN 0 WHEN 'new' THEN 1 WHEN 'viewed' THEN 2 "
        "WHEN 'applied' THEN 3 WHEN 'passed' THEN 4 END, id",
        params,
    ).fetchall()

    if not rows:
        print("No listings found.")
        return

    print(f"\n{'ID':>3}  {'ST':3}  {'SCORE':5}  H  L  {'AVAIL':<6}  {'PRICE':>7}  {'LOC':<22}  TITLE")
    print("─" * 92)
    for r in rows:
        icon   = STATUS_ICONS.get(r["status"], "[ ]")
        stars  = score_stars(neighborhood_score(r["location"] or ""))
        hs     = "H" if row_is_house(r) else " "
        ls     = "L" if row_has_laundry(r) else " "
        avail  = (row_available(r) or "")[:6]
        price  = fmt_price(r["price"])
        loc    = (r["location"] or "—")[:22]
        title  = (r["title"] or r["url"])[:38]
        line   = f"{r['id']:>3}  {icon}  {stars}  {hs}  {ls}  {avail:<6}  {price:>7}  {loc:<22}  {title}"
        print(color(r["status"], line))

    print()
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = "  ".join(f"{STATUS_ICONS[s]} {s}: {counts[s]}" for s in STATUS_ORDER if s in counts)
    print(f"  {len(rows)} listings  |  {summary}")
    print(f"  H = house/townhouse/duplex unit   L = in-unit laundry\n")


def cmd_show(args):
    conn = db_connect()
    r = conn.execute("SELECT * FROM listings WHERE id = ?", (args.id,)).fetchone()
    if not r:
        print(f"No listing with id {args.id}")
        sys.exit(1)
    score   = neighborhood_score(r["location"] or "")
    hs      = "Yes" if row_is_house(r) else "No"
    ls      = "Yes" if row_has_laundry(r) else "Unknown"
    print(f"""
  #{r['id']}  {STATUS_ICONS.get(r['status'])} {r['status'].upper()}
  ─────────────────────────────────────────
  Title     : {r['title'] or '—'}
  Price     : {fmt_price(r['price'])}
  Location  : {r['location'] or '—'}
  Source    : {r['source'] or '—'}
  Nbhd score: {score_stars(score)} ({score}/5)
  House unit: {hs}
  In-unit laundry: {ls}
  Available : {row_available(r) or '—'}
  East Camb : {'YES — top priority' if row_is_east_cam(r) else 'no'}
  Commute   : {f"walk {r['walk_min']}m / bike {r['bike_min']}m / subway {r['transit_min']}m / bus {r['bus_min']}m" if r['walk_min'] is not None else '— (run: track.py commute)'}
  URL       : {r['url']}
  Added     : {r['added_on']}
  Updated   : {r['updated_on']}
  Notes     :
{_indent_notes(r['notes'])}
""")


def _indent_notes(notes):
    if not notes or not notes.strip():
        return "    (none)"
    return "\n".join(f"    {line}" for line in notes.strip().splitlines())


def cmd_set_status(args, status):
    conn = db_connect()
    r = conn.execute("SELECT id, title FROM listings WHERE id = ?", (args.id,)).fetchone()
    if not r:
        print(f"No listing with id {args.id}")
        sys.exit(1)
    conn.execute("UPDATE listings SET status = ?, updated_on = ? WHERE id = ?", (status, now(), args.id))
    conn.commit()
    print(f"{STATUS_ICONS[status]}  #{args.id} {(r['title'] or '')[:50]} → {status}")


def cmd_note(args):
    conn = db_connect()
    r = conn.execute("SELECT id, notes FROM listings WHERE id = ?", (args.id,)).fetchone()
    if not r:
        print(f"No listing with id {args.id}")
        sys.exit(1)
    new_notes = ((r["notes"] or "") + f"\n[{now()}] {args.text}").strip()
    conn.execute("UPDATE listings SET notes = ?, updated_on = ? WHERE id = ?", (new_notes, now(), args.id))
    conn.commit()
    print(f"Note added to #{args.id}")


def cmd_edit(args):
    conn = db_connect()
    r = conn.execute("SELECT * FROM listings WHERE id = ?", (args.id,)).fetchone()
    if not r:
        print(f"No listing with id {args.id}")
        sys.exit(1)
    title    = args.title    if args.title    is not None else r["title"]
    price    = int(args.price) if args.price  is not None else r["price"]
    location = args.location if args.location is not None else r["location"]
    fp = fingerprint(title, location, price)
    conn.execute(
        "UPDATE listings SET title=?, price=?, location=?, fingerprint=?, updated_on=? WHERE id=?",
        (title, price, location, fp, now(), args.id),
    )
    conn.commit()
    print(f"Updated #{args.id}")


def cmd_delete(args):
    conn = db_connect()
    r = conn.execute("SELECT id, title FROM listings WHERE id = ?", (args.id,)).fetchone()
    if not r:
        print(f"No listing with id {args.id}")
        sys.exit(1)
    confirm = ask(f"Delete #{args.id} '{r['title'] or r['id']}'? [y/N] ")
    if confirm.lower() == "y":
        conn.execute("DELETE FROM listings WHERE id = ?", (args.id,))
        conn.commit()
        print(f"Deleted #{args.id}")
    else:
        print("Cancelled.")


def _print_fetched(results, label=""):
    if label:
        print(f"\n{label}")
    if not results:
        print("  No results.")
        return
    print(f"\n  {'SCORE':5}  H  {'PRICE':>7}  {'DATE':10}  TITLE")
    print("  " + "─" * 76)
    for r in results:
        stars = score_stars(r.get("_score", 0))
        hs    = "H" if r.get("_house") else " "
        price = fmt_price(r.get("price_int"))
        date  = (r.get("date") or "")[:10]
        src   = f"[{r.get('source','')[:2].upper()}]" if r.get("source") else "    "
        title = (r.get("title") or "")[:45] or r["url"][:60]
        print(f"  {stars}  {hs}  {price:>7}  {date:10}  {src} {title}")
        print(f"           {r['url']}")
        print()
    print(f"  H = likely house/townhouse/duplex unit")


def _enrich(results):
    """Add _score/_house/_laundry/_avail fields to raw listing dicts.
    Respects pre-set flags (e.g. from a houses-only search page).
    Rank: neighborhood (x2) + house preference (x2) + laundry + Sept-1 mention."""
    for r in results:
        r["_score"]   = neighborhood_score(r.get("location", ""))
        r["_house"]   = r.get("_house") or house_score((r.get("title") or "") + " " + (r.get("location") or ""))
        r["_laundry"] = r.get("_laundry") or laundry_score(r.get("title") or "")
        # Normalize whatever the scraper captured (ISO date, epoch, "Available
        # Now", month text…) to a short display string. Fall back to a Sept-1
        # mention in the title only when no real availability date was found.
        avail = parse_move_in(r.get("_avail"))
        if not avail and sept_score(r.get("title") or ""):
            avail = "Sep 1"
        r["_avail"]   = avail
        r["_sept"]    = 1 if sept_score(avail) else 0
        r["_ec"]      = row_is_east_cam(r)
    results.sort(key=lambda r: -(r["_ec"] * 20 + r["_score"] * 2 + r["_house"] * 2 + r["_laundry"] + r["_sept"]))
    return results


def _save_listings(conn, results, source):
    ts = now()
    added, skipped = [], []
    for r in results:
        title    = r.get("title") or ""
        location = r.get("location") or ""
        price    = r.get("price_int")
        fp = fingerprint(title, location, price)
        dup, dup_id = is_duplicate(conn, r["url"], fp)
        if dup:
            skipped.append(dup_id)
            continue
        try:
            cur = conn.execute(
                "INSERT INTO listings "
                "(url, title, price, location, status, notes, source, is_house, image, "
                " has_laundry, available, lat, lon, geo_src, fingerprint, added_on, updated_on, listed_on, photos) "
                "VALUES (?, ?, ?, ?, 'new', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["url"], title or None, price, location or None, source,
                 int(r.get("_house") or 0), r.get("image") or "",
                 int(r.get("_laundry") or 0), r.get("_avail") or "",
                 r.get("_lat"), r.get("_lon"),
                 "addr" if r.get("_lat") else "", fp, ts, ts, r.get("_listed_on"),
                 _json.dumps(r["_photos"]) if r.get("_photos") else None),
            )
            conn.commit()
            r["_id"] = cur.lastrowid
            added.append(r)
        except Exception:
            pass
    return added, skipped


def backfill_listing_dates(conn, results):
    """Fill empty listed_on / available on EXISTING rows from freshly scraped
    values. Re-scrapes skip duplicates, so without this the date columns added
    to already-saved listings would never populate. Purely additive: only writes
    a column that is currently blank, so manual edits and prior values are kept.
    Returns the number of rows updated."""
    n = 0
    for r in results:
        listed = r.get("_listed_on")
        avail  = r.get("_avail")
        photos = r.get("_photos")
        if not listed and not avail and not photos:
            continue
        row = conn.execute(
            "SELECT id, listed_on, available, photos FROM listings WHERE url=?", (r["url"],)
        ).fetchone()
        if not row:
            continue
        sets, params = [], []
        if listed and not (row["listed_on"] or ""):
            sets.append("listed_on=?"); params.append(listed)
        if avail and not (row["available"] or ""):
            sets.append("available=?"); params.append(avail)
        if photos and not (row["photos"] or ""):
            sets.append("photos=?"); params.append(_json.dumps(photos))
        if not sets:
            continue
        params.append(row["id"])
        conn.execute(f"UPDATE listings SET {', '.join(sets)} WHERE id=?", params)
        n += 1
    if n:
        conn.commit()
    return n


_REMOVED_PHRASES = re.compile(
    r"(this posting has (been )?(deleted|expired|removed)|posting has been flagged|"
    r"no longer available|listing (is )?(no longer|has been removed|not available)|"
    r"this home is no longer|is no longer (available|active)|"
    r"page (not found|isn'?t available)|we can'?t find (this|that) (page|listing))",
    re.IGNORECASE,
)
_BLOCKED_PHRASES = re.compile(
    r"(access to this page has been denied|access denied|pardon our interruption|"
    r"px-captcha|are you a human|verify you are|unusual traffic|please enable javascript)",
    re.IGNORECASE,
)


def check_listing_removed(url):
    """Has this listing been taken down? Returns True (removed), False (live), or
    None (couldn't tell — bot-blocked, network error, or login wall). Conservative
    on purpose: only returns True on a clear 404/410 or an explicit removal phrase,
    so a bot-block is never mistaken for a removal."""
    headers = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
               "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            final_url = resp.geturl()
            if resp.headers.get("Content-Encoding", "") == "gzip":
                raw = gzip.decompress(raw)
            html = raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return True if e.code in (404, 410) else None   # 404/410 = gone; others unknown
    except Exception:
        return None
    if _BLOCKED_PHRASES.search(html[:6000]):
        return None                                     # bot wall — can't tell
    if _REMOVED_PHRASES.search(html):
        return True
    # Craigslist: a deleted post redirects to the search page or a removal notice
    if "craigslist.org" in url and "/search/" in final_url:
        return True
    return False


def mark_delisted(conn, listing_id, removed):
    """Set/clear the delisted flag for a listing."""
    if removed:
        conn.execute("UPDATE listings SET delisted=1, delisted_on=?, updated_on=? WHERE id=?",
                     (now(), now(), listing_id))
    else:
        conn.execute("UPDATE listings SET delisted=0, delisted_on='' WHERE id=?", (listing_id,))
    conn.commit()


def prune_removed(conn, log=print, only_active=True):
    """Check live listings' URLs and flag the ones taken down. Returns
    (checked, newly_removed). Skips already-delisted rows and bot-blocked /
    ambiguous responses (those stay as-is)."""
    where = "WHERE COALESCE(delisted,0)=0" if only_active else ""
    rows = conn.execute(f"SELECT id, url, source FROM listings {where}").fetchall()
    checked = removed = 0
    for r in rows:
        verdict = check_listing_removed(r["url"])
        checked += 1
        if verdict is True:
            mark_delisted(conn, r["id"], True)
            removed += 1
            log(f"  removed: #{r['id']} {r['url']}")
    log(f"  checked {checked}, newly flagged removed: {removed}")
    return checked, removed


_ADDR_SUFFIX = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr",
    "lane": "ln", "boulevard": "blvd", "court": "ct", "place": "pl",
    "terrace": "ter", "square": "sq", "parkway": "pkwy", "highway": "hwy",
    "circle": "cir",
    # spelled-out ordinals → numeric, so "Fifth St" == "5th St"
    "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th",
    "fifth": "5th", "sixth": "6th", "seventh": "7th", "eighth": "8th",
    "ninth": "9th", "tenth": "10th", "eleventh": "11th", "twelfth": "12th",
}


def _addr_key(addr):
    """Canonicalize a street address: lowercase, standard suffixes/ordinals, and
    drop a trailing letter on the house number ('208A Washington' → '208 …') so
    address variants of the same building collapse together."""
    a = addr.lower().replace(".", "")
    a = " ".join(_ADDR_SUFFIX.get(w, w) for w in a.split())
    a = re.sub(r"[^\w ]", "", a)
    a = re.sub(r"\s+", " ", a).strip()
    return re.sub(r"^(\d+)[a-z]\b", r"\1", a)   # 208a → 208


def _norm_building(r):
    """Normalized street address WITHOUT the unit — groups units of one building.
    '' when there's no street address (so such rows are never grouped)."""
    addr = extract_address(r["location"] or "") or extract_address(r["title"] or "")
    return _addr_key(addr) if addr else ""


def _norm_addr(r):
    """Normalized street address (+ unit) for duplicate detection, or '' when no
    street address is present (city-only listings are never grouped)."""
    b = _norm_building(r)
    if not b:
        return ""
    unit = row_unit(r)
    return f"{b} #{unit.lower()}" if unit else b


def _completeness(r):
    """How much real data a row carries — used to pick the canonical of a dup set."""
    score = 0
    for col in ("sqft", "beds", "photos", "walk_min", "amen_text"):
        try:
            if r[col]:
                score += 1
        except (KeyError, IndexError):
            pass
    if (r["geo_src"] or "") == "addr":
        score += 1
    return score


def mark_duplicates(conn, log=print):
    """Group active listings by normalized address and flag all but the most
    complete in each group as duplicate (hidden in the UI). Idempotent."""
    rows = conn.execute("SELECT * FROM listings WHERE COALESCE(delisted,0)=0").fetchall()
    groups = {}
    for r in rows:
        k = _norm_addr(r)
        if k:
            groups.setdefault(k, []).append(r)
    conn.execute("UPDATE listings SET duplicate=0, dup_of=NULL")
    dups = 0
    for g in groups.values():
        if len(g) < 2:
            continue
        canon = max(g, key=lambda r: (_completeness(r), -r["id"]))
        for r in g:
            if r["id"] != canon["id"]:
                conn.execute("UPDATE listings SET duplicate=1, dup_of=? WHERE id=?",
                             (canon["id"], r["id"]))
                dups += 1
    conn.commit()
    naddr = sum(1 for g in groups.values() if len(g) > 1)
    log(f"  {dups} duplicate listing(s) across {naddr} shared address(es)")
    return dups


def record_scrape(conn, source, total=0, new=0, error=""):
    """Stamp the time a source was last scraped (whether or not it found
    anything), so the UI can show 'last scraped' per site."""
    try:
        conn.execute(
            "INSERT INTO scrape_runs (source, last_run, last_total, last_new, last_error) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET "
            "last_run=excluded.last_run, last_total=excluded.last_total, "
            "last_new=excluded.last_new, last_error=excluded.last_error",
            (source, now(), int(total or 0), int(new or 0), error or ""),
        )
        conn.commit()
    except Exception:
        pass


def scrape_runs(conn):
    """{source: {last_run, last_total, last_new, last_error}} for display."""
    try:
        rows = conn.execute(
            "SELECT source, last_run, last_total, last_new, last_error FROM scrape_runs"
        ).fetchall()
    except Exception:
        return {}
    return {r["source"]: {"last_run": r["last_run"], "last_total": r["last_total"],
                          "last_new": r["last_new"], "last_error": r["last_error"]}
            for r in rows}


def cmd_fetch_cl(args):
    print(f"Fetching Craigslist (house/duplex/townhouse types)...\n")
    results = scrape_craigslist()

    if not results:
        print("No results. Craigslist may be blocking requests.")
        if not _has_playwright():
            print("Tip: pip install playwright && playwright install chromium")
        print(f"\nBrowse manually:\n  House types: {SEARCH_URL}\n  All types:   {SEARCH_URL_ALL}")
        return

    _enrich(results)
    conn = db_connect()
    backfill_listing_dates(conn, results)
    new, seen = [], []
    for r in results:
        fp = fingerprint(r.get("title"), r.get("location"), r.get("price_int"))
        dup, _ = is_duplicate(conn, r["url"], fp)
        (seen if dup else new).append(r)

    record_scrape(conn, "craigslist", len(results), len(new))
    _print_fetched(new, f"NEW listings ({len(new)} of {len(results)}, {len(seen)} already in DB):")
    if not new:
        print("  Nothing new since last update.")


def cmd_fetch_apts(args):
    if not _has_playwright():
        print("Playwright is required for Apartments.com scraping.")
        print("Install: pip install playwright && playwright install chromium")
        sys.exit(1)

    print("Fetching Apartments.com (houses + 1BR in Cambridge/Somerville)...\n")
    try:
        results = asyncio.run(_scrape_apts_pw())
    except Exception as e:
        print(f"  [!] Apartments.com scrape failed: {e}")
        return

    if not results:
        print("No results returned. Try browsing manually via the search links.")
        return

    _enrich(results)
    conn = db_connect()
    backfill_listing_dates(conn, results)
    new, seen = [], []
    for r in results:
        fp = fingerprint(r.get("title"), r.get("location"), r.get("price_int"))
        dup, _ = is_duplicate(conn, r["url"], fp)
        (seen if dup else new).append(r)

    record_scrape(conn, "apartments", len(results), len(new))
    _print_fetched(new, f"NEW Apartments.com listings ({len(new)} of {len(results)}, {len(seen)} already in DB):")

    if new:
        save = ask(f"\nSave {len(new)} listing(s) to DB? [y/N] ")
        if save.lower() == "y":
            added, _ = _save_listings(conn, new, "apartments")
            print(f"Saved {len(added)} listings.")


def cmd_fetch_zillow(args):
    if not _has_playwright():
        print("Playwright is required for Zillow scraping.")
        print("Install: pip install playwright && playwright install chromium")
        sys.exit(1)

    print("Fetching Zillow (Cambridge + Somerville 1BR, $2000-2800)...\n")
    try:
        results = asyncio.run(_scrape_zillow_pw())
    except Exception as e:
        print(f"  [!] Zillow scrape failed: {e}")
        return

    if not results:
        print("No results (bot-blocked or none match). Try again later or run interactively.")
        return

    _enrich(results)
    conn = db_connect()
    backfill_listing_dates(conn, results)
    new, seen = [], []
    for r in results:
        fp = fingerprint(r.get("title"), r.get("location"), r.get("price_int"))
        dup, _dupid = is_duplicate(conn, r["url"], fp)
        (seen if dup else new).append(r)

    record_scrape(conn, "zillow", len(results), len(new))
    _print_fetched(new, f"NEW Zillow listings ({len(new)} of {len(results)}, {len(seen)} already in DB):")

    if new:
        save = ask(f"\nSave {len(new)} listing(s) to DB? [y/N] ")
        if save.lower() == "y":
            added, _ = _save_listings(conn, new, "zillow")
            print(f"Saved {len(added)} listings.")


def cmd_fetch_hotpads(args):
    if not _has_playwright():
        print("Playwright is required for HotPads scraping.")
        print("Install: pip install playwright && playwright install chromium")
        sys.exit(1)

    print("Fetching HotPads (Cambridge + Somerville 1BR, <= $2800)...\n")
    try:
        results = asyncio.run(_scrape_hotpads_pw())
    except Exception as e:
        print(f"  [!] HotPads scrape failed: {e}")
        return

    if not results:
        print("No results (bot-blocked or none match). If blocked, run this once in a "
              "terminal to solve the captcha; clearance persists in .hotpads_profile.")
        return

    _enrich(results)
    conn = db_connect()
    backfill_listing_dates(conn, results)
    new, seen = [], []
    for r in results:
        fp = fingerprint(r.get("title"), r.get("location"), r.get("price_int"))
        dup, _dupid = is_duplicate(conn, r["url"], fp)
        (seen if dup else new).append(r)

    record_scrape(conn, "hotpads", len(results), len(new))
    _print_fetched(new, f"NEW HotPads listings ({len(new)} of {len(results)}, {len(seen)} already in DB):")

    if new:
        save = ask(f"\nSave {len(new)} listing(s) to DB? [y/N] ")
        if save.lower() == "y":
            added, _ = _save_listings(conn, new, "hotpads")
            print(f"Saved {len(added)} listings.")


def cmd_fetch_rent(args):
    print("Fetching Rent.com (Cambridge + Somerville 1BR, <= $2800)...\n")
    try:
        results = scrape_rent()
    except Exception as e:
        print(f"  [!] Rent.com scrape failed: {e}")
        return

    if not results:
        print("No results (none match or page layout changed). Browse manually:")
        for _label, url in RENT_URLS:
            print(f"  {url}")
        return

    _enrich(results)
    conn = db_connect()
    backfill_listing_dates(conn, results)
    new, seen = [], []
    for r in results:
        fp = fingerprint(r.get("title"), r.get("location"), r.get("price_int"))
        dup, _dupid = is_duplicate(conn, r["url"], fp)
        (seen if dup else new).append(r)

    record_scrape(conn, "rent", len(results), len(new))
    _print_fetched(new, f"NEW Rent.com listings ({len(new)} of {len(results)}, {len(seen)} already in DB):")

    if new:
        save = ask(f"\nSave {len(new)} listing(s) to DB? [y/N] ")
        if save.lower() == "y":
            added, _ = _save_listings(conn, new, "rent")
            print(f"Saved {len(added)} listings.")


def cmd_fetch_fb(args):
    if not _has_playwright():
        print("Playwright is required for Facebook Marketplace scraping.")
        print("Install: pip install playwright && playwright install chromium")
        sys.exit(1)

    print(f"Fetching Facebook Marketplace...\n")
    try:
        results = asyncio.run(_scrape_fb_pw())
    except Exception as e:
        print(f"  [!] Facebook scrape failed: {e}")
        return

    if not results:
        print(f"No results. Browse manually: {FB_SEARCH_URL}")
        return

    _enrich(results)
    conn = db_connect()
    backfill_listing_dates(conn, results)
    new, seen = [], []
    for r in results:
        fp = fingerprint(r.get("title"), r.get("location"), r.get("price_int"))
        dup, _ = is_duplicate(conn, r["url"], fp)
        (seen if dup else new).append(r)

    record_scrape(conn, "facebook", len(results), len(new))
    _print_fetched(new, f"NEW FB listings ({len(new)} of {len(results)}, {len(seen)} already in DB):")

    if new:
        save = ask(f"\nSave {len(new)} listing(s) to DB? [y/N] ")
        if save.lower() == "y":
            added, _ = _save_listings(conn, new, "facebook")
            print(f"Saved {len(added)} listings.")


def cmd_update(args):
    print(f"[{now()}] Fetching from Craigslist...")
    results = scrape_craigslist()

    if not results:
        print("No results. Check manually:")
        print(f"  {SEARCH_URL}")
        return

    _enrich(results)
    conn = db_connect()
    backfill_listing_dates(conn, results)
    added, skipped = _save_listings(conn, results, "craigslist")

    print(f"  {len(results)} fetched  |  {len(added)} new  |  {len(skipped)} duplicates skipped\n")

    if added:
        added.sort(key=lambda r: -(r.get("_score", 0) * 2 + r.get("_house", 0)))
        print("  NEW LISTINGS ADDED:")
        print(f"  {'ID':>4}  {'SCORE':5}  H  {'PRICE':>7}  TITLE")
        print("  " + "─" * 64)
        for r in added:
            stars = score_stars(r.get("_score", 0))
            hs    = "H" if r.get("_house") else " "
            price = fmt_price(r.get("price_int"))
            title = (r.get("title") or r["url"])[:45]
            print(f"  #{r['_id']:<4} {stars}  {hs}  {price:>7}  {title}")
            print(f"         {r['url']}")
        print()
    else:
        print("  No new listings found.")


def _build_summary_html(runs):
    """
    runs: list of dicts with keys: source, label, url, total, new_count, error
    """
    rows = ""
    for r in runs:
        if r.get("error"):
            status_cell = f'<td colspan="2" style="color:#dc2626">{r["error"]}</td>'
        else:
            status_cell = (
                f'<td class="num">{r["total"]}</td>'
                f'<td class="num new-count">+{r["new_count"]} new</td>'
            )
        rows += (
            f'<tr>'
            f'<td><strong>{r["label"]}</strong></td>'
            f'{status_cell}'
            f'</tr>'
            f'<tr><td colspan="3" class="ss-url"><a href="{r["url"]}" target="_blank">{r["url"]}</a></td></tr>'
        )
    return (
        f'<div class="search-summary">'
        f'<h2>Today\'s Search</h2>'
        f'<table class="ss">'
        f'<tr><th>Source</th><th>Results</th><th>New</th></tr>'
        f'{rows}'
        f'</table>'
        f'</div>'
    )


def cmd_enrich_apts(args):
    """Fetch Apartments.com detail pages to fill beds/baths/sqft/move-in."""
    if not _has_playwright():
        print("Playwright is required. Install: pip install playwright && playwright install chromium")
        sys.exit(1)
    conn = db_connect()
    print("Enriching Apartments.com listings from detail pages (opens a browser)...")
    enrich_apts_details(conn, log=print, only_missing=not getattr(args, "all", False))


def cmd_dedupe(args):
    """Flag same-address listings as duplicates (hidden in the UI)."""
    conn = db_connect()
    print("Flagging duplicate listings by address...")
    mark_duplicates(conn, log=print)
    total = conn.execute("SELECT COUNT(*) FROM listings WHERE duplicate=1").fetchone()[0]
    print(f"Done. {total} listing(s) flagged as duplicates.")


def cmd_prune(args):
    """Check listing URLs and flag the ones removed by the author (hidden in UI)."""
    conn = db_connect()
    print("Checking listings for removals (this hits each URL)...")
    checked, removed = prune_removed(conn, log=print)
    total_removed = conn.execute("SELECT COUNT(*) FROM listings WHERE delisted=1").fetchone()[0]
    print(f"\nDone. {removed} newly removed; {total_removed} total flagged as removed.")


def cmd_daily(args):
    """Run all scrapers, save a dated HTML summary to ~/Desktop."""
    if getattr(args, "if_stale", False):
        last = (db_connect().execute("SELECT MAX(last_run) FROM scrape_runs").fetchone()[0] or "")
        if last[:10] == now()[:10]:
            print(f"Already scraped today ({last}). Skipping (--if-stale).")
            return
    if not _has_playwright():
        print("Playwright is required for daily scraping.")
        print("Install: pip install playwright && playwright install chromium")
        sys.exit(1)

    conn = db_connect()
    runs = []
    all_new = []

    # ── Craigslist ──
    print("1/6  Craigslist...")
    try:
        cl_results = scrape_craigslist()
        _enrich(cl_results)
        backfill_listing_dates(conn, cl_results)
        new, seen = [], []
        for r in cl_results:
            fp = fingerprint(r.get("title"), r.get("location"), r.get("price_int"))
            dup, _dupid = is_duplicate(conn, r["url"], fp)
            (seen if dup else new).append(r)
        added, _ = _save_listings(conn, new, "craigslist")
        all_new.extend(added)
        runs.append({"label": "Craigslist (house types)", "url": SEARCH_URL,
                     "total": len(cl_results), "new_count": len(added)})
        print(f"     {len(cl_results)} found, {len(added)} new")
    except Exception as e:
        runs.append({"label": "Craigslist", "url": SEARCH_URL, "total": 0, "new_count": 0, "error": str(e)})
        print(f"     failed: {e}")

    # ── Apartments.com ──
    print("2/6  Apartments.com...")
    try:
        apts_results = asyncio.run(_scrape_apts_pw())
        _enrich(apts_results)
        backfill_listing_dates(conn, apts_results)
        new, seen = [], []
        for r in apts_results:
            fp = fingerprint(r.get("title"), r.get("location"), r.get("price_int"))
            dup, _dupid = is_duplicate(conn, r["url"], fp)
            (seen if dup else new).append(r)
        added, _ = _save_listings(conn, new, "apartments")
        all_new.extend(added)
        runs.append({"label": "Apartments.com (Cambridge + Somerville houses + 1BR)",
                     "url": APTS_URLS[0][1], "total": len(apts_results), "new_count": len(added)})
        print(f"     {len(apts_results)} found, {len(added)} new")
    except Exception as e:
        runs.append({"label": "Apartments.com", "url": APTS_URLS[0][1], "total": 0, "new_count": 0, "error": str(e)})
        print(f"     failed: {e}")

    # ── Zillow ──
    print("3/6  Zillow...")
    try:
        z_results = asyncio.run(_scrape_zillow_pw())
        _enrich(z_results)
        backfill_listing_dates(conn, z_results)
        added, _skipped = _save_listings(conn, z_results, "zillow")
        all_new.extend(added)
        runs.append({"label": "Zillow (Cambridge + Somerville 1BR)", "url": ZILLOW_URLS[0][1],
                     "total": len(z_results), "new_count": len(added)})
        print(f"     {len(z_results)} found, {len(added)} new")
    except Exception as e:
        runs.append({"label": "Zillow", "url": ZILLOW_URLS[0][1], "total": 0, "new_count": 0, "error": str(e)})
        print(f"     failed: {e}")

    # ── Rent.com ──
    print("4/6  Rent.com...")
    try:
        rent_results = scrape_rent()
        _enrich(rent_results)
        backfill_listing_dates(conn, rent_results)
        new, seen = [], []
        for r in rent_results:
            fp = fingerprint(r.get("title"), r.get("location"), r.get("price_int"))
            dup, _dupid = is_duplicate(conn, r["url"], fp)
            (seen if dup else new).append(r)
        added, _ = _save_listings(conn, new, "rent")
        all_new.extend(added)
        runs.append({"label": "Rent.com (Cambridge + Somerville 1BR)", "url": RENT_URLS[0][1],
                     "total": len(rent_results), "new_count": len(added)})
        print(f"     {len(rent_results)} found, {len(added)} new")
    except Exception as e:
        runs.append({"label": "Rent.com", "url": RENT_URLS[0][1], "total": 0, "new_count": 0, "error": str(e)})
        print(f"     failed: {e}")

    # ── HotPads ──
    print("5/6  HotPads...")
    try:
        hp_results = asyncio.run(_scrape_hotpads_pw())
        _enrich(hp_results)
        backfill_listing_dates(conn, hp_results)
        new, seen = [], []
        for r in hp_results:
            fp = fingerprint(r.get("title"), r.get("location"), r.get("price_int"))
            dup, _dupid = is_duplicate(conn, r["url"], fp)
            (seen if dup else new).append(r)
        added, _ = _save_listings(conn, new, "hotpads")
        all_new.extend(added)
        runs.append({"label": "HotPads (Cambridge + Somerville 1BR)", "url": HOTPADS_SEARCH_URL,
                     "total": len(hp_results), "new_count": len(added)})
        print(f"     {len(hp_results)} found, {len(added)} new")
    except Exception as e:
        runs.append({"label": "HotPads", "url": HOTPADS_SEARCH_URL, "total": 0, "new_count": 0, "error": str(e)})
        print(f"     failed: {e}")

    # ── Facebook Marketplace ──
    if getattr(args, "skip_fb", False):
        print("6/6  Facebook Marketplace — skipped (--skip-fb)")
        runs.append({"label": "Facebook Marketplace", "url": FB_SEARCH_URL,
                     "total": 0, "new_count": 0, "error": "skipped (--skip-fb)"})
        fb_skipped = True
    else:
        fb_skipped = False
    if not fb_skipped:
        try:
            fb_results = asyncio.run(_scrape_fb_pw())
            _enrich(fb_results)
            backfill_listing_dates(conn, fb_results)
            new, seen = [], []
            for r in fb_results:
                fp = fingerprint(r.get("title"), r.get("location"), r.get("price_int"))
                dup, _dupid = is_duplicate(conn, r["url"], fp)
                (seen if dup else new).append(r)
            added, _ = _save_listings(conn, new, "facebook")
            all_new.extend(added)
            runs.append({"label": "Facebook Marketplace", "url": FB_SEARCH_URL,
                         "total": len(fb_results), "new_count": len(added)})
            print(f"     {len(fb_results)} found, {len(added)} new")
        except Exception as e:
            runs.append({"label": "Facebook Marketplace", "url": FB_SEARCH_URL, "total": 0, "new_count": 0, "error": str(e)})
            print(f"     failed: {e}")

    # ── Stamp each source's last-scrape time (derived from the runs above) ──
    for run in runs:
        label = run["label"].lower()
        src = next((s for s in ("craigslist", "apartments", "zillow", "rent", "hotpads", "facebook")
                    if s in label or (s == "facebook" and "facebook" in label)), None)
        if src:
            record_scrape(conn, src, run.get("total", 0), run.get("new_count", 0), run.get("error", ""))

    # ── Enrich Apartments.com listings with detail-page beds/baths/sqft ──
    print("Enriching Apartments.com detail data...")
    try:
        enrich_apts_details(conn, log=lambda m: print(f"     {m}"))
    except Exception as e:
        print(f"     apts detail enrichment failed: {e}")

    # ── Flag listings removed by their author (hidden in the UI) ──
    print("Checking for removed listings...")
    try:
        prune_removed(conn, log=lambda m: print(f"     {m}"))
    except Exception as e:
        print(f"     removal check failed: {e}")

    # ── Flag same-address duplicates (hidden in the UI) ──
    print("Flagging duplicate listings...")
    try:
        mark_duplicates(conn, log=lambda m: print(f"     {m}"))
    except Exception as e:
        print(f"     dedupe failed: {e}")

    # ── Commute times for new listings ──
    try:
        compute_missing_commutes(conn, log=lambda m: print(f"     {m}"))
    except Exception as e:
        print(f"     commute computation failed: {e}")
    try:
        backfill_sv_headings(conn, log=lambda m: print(f"     {m}"))
    except Exception as e:
        print(f"     street-view heading backfill failed: {e}")
    try:
        backfill_derived(conn)
    except Exception as e:
        print(f"     metadata backfill failed: {e}")

    # ── Build HTML ──
    all_rows = conn.execute(
        "SELECT * FROM listings ORDER BY "
        "CASE status WHEN 'interested' THEN 0 WHEN 'applied' THEN 1 WHEN 'new' THEN 2 "
        "WHEN 'viewed' THEN 3 WHEN 'passed' THEN 4 END, id"
    ).fetchall()

    new_ids = {n["_id"] for n in all_new if n.get("_id")}
    sections_html = _render_sections(all_rows, new_ids)
    if not sections_html:
        sections_html = '<div class="section"><p class="empty">No listings in DB yet.</p></div>'

    links_html = "\n".join(
        f'<a href="{url}" target="_blank">{name}</a>' for name, url in _SEARCH_LINKS
    )
    hood_rows = "".join(
        f'<tr><td><strong>{n["name"]}</strong></td>'
        f'<td>{n["transit"]}</td><td>{n["avg"]}</td>'
        f'<td class="score-stars">{"★" * n["score"] + "☆" * (5 - n["score"])}</td></tr>'
        for n in NEIGHBORHOODS
    )

    date_str     = datetime.now().strftime("%B %d, %Y %H:%M")
    summary_html = _build_summary_html(runs)
    html = _HTML.format(
        date=date_str,
        search_summary=summary_html,
        sections=sections_html,
        links=links_html,
        neighborhoods=hood_rows,
    )

    # Save dated file to the htmls/ archive directory
    date_slug = datetime.now().strftime("%Y-%m-%d")
    html_dir  = os.path.join(SCRIPT_DIR, "htmls")
    os.makedirs(html_dir, exist_ok=True)
    out_path  = os.path.join(html_dir, f"apt-search-{date_slug}.html")
    with open(out_path, "w") as f:
        f.write(html)

    total_new = sum(r.get("new_count", 0) for r in runs)
    print(f"\nDone — {total_new} new listings found across all sources.")
    print(f"Saved → {out_path}")
    print(f"Open  → open \"{out_path}\"")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _copy_listings(src, dst, wipe=False):
    """Copy every listings row from src connection to dst (INSERT OR REPLACE,
    preserving ids). Returns number of rows copied."""
    cols = [r[1] for r in src.execute("PRAGMA table_info(listings)").fetchall()]
    rows = src.execute("SELECT * FROM listings").fetchall()
    if wipe:
        dst.execute("DELETE FROM listings")
        dst.commit()
    collist = ",".join(cols)
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT OR REPLACE INTO listings ({collist}) VALUES ({placeholders})"
    n = 0
    for r in rows:
        dst.execute(sql, tuple(r[c] for c in cols))
        n += 1
    dst.commit()
    return n


def cmd_db_status(args):
    creds = _load_turso()
    if creds:
        print(f"Mode:     SHARED (Turso)\nDatabase: {creds[0]}")
    else:
        print(f"Mode:     LOCAL\nDatabase: {DB_PATH}")
    try:
        conn = db_connect()
        n = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        print(f"Listings: {n}")
        print("Connection: OK")
    except Exception as e:
        print(f"Connection: FAILED — {e}")
        sys.exit(1)


def cmd_db_push(args):
    if not _load_turso():
        print("No remote configured. Add a .turso_key file (see README) first.")
        sys.exit(1)
    if not os.path.exists(DB_PATH):
        print(f"No local DB at {DB_PATH} — nothing to push.")
        sys.exit(1)
    src = sqlite3.connect(DB_PATH)
    src.row_factory = sqlite3.Row
    dst = db_connect()  # remote (schema ensured)
    n = _copy_listings(src, dst, wipe=args.wipe)
    total = dst.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    print(f"Pushed {n} local row(s). Remote now has {total} listing(s).")


def cmd_db_pull(args):
    if not _load_turso():
        print("No remote configured — nothing to pull.")
        sys.exit(1)
    src = db_connect()  # remote
    if os.path.exists(DB_PATH):
        bak = DB_PATH + ".bak"
        import shutil
        shutil.copy2(DB_PATH, bak)
        print(f"Backed up existing local DB to {bak}")
    dst = sqlite3.connect(DB_PATH)
    dst.row_factory = sqlite3.Row
    _db_init_schema(dst)
    n = _copy_listings(src, dst, wipe=args.wipe)
    total = dst.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    print(f"Pulled {n} remote row(s) into local DB. Local now has {total} listing(s).")


def main():
    p = argparse.ArgumentParser(description="Apartment listing tracker")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("add")
    a.add_argument("url")
    a.add_argument("--title", "-t")
    a.add_argument("--price", "-p", type=int)
    a.add_argument("--location", "-l")
    a.add_argument("--notes", "-n")

    sub.add_parser("list").add_argument("--status", "-s", choices=STATUS_ORDER)
    sub.add_parser("show").add_argument("id", type=int)
    sub.add_parser("fetch-cl")
    sub.add_parser("fetch").set_defaults(cmd="fetch-cl")  # back-compat
    sub.add_parser("fetch-fb")
    sub.add_parser("fetch-apts")
    sub.add_parser("fetch-zillow")
    sub.add_parser("fetch-rent")
    sub.add_parser("fetch-hotpads")
    sub.add_parser("enrich-apts").add_argument("--all", action="store_true",
                                               help="re-fetch all (default: only rows missing sqft)")
    sub.add_parser("prune")
    sub.add_parser("dedupe")
    sub.add_parser("update")
    sub.add_parser("html")
    _daily = sub.add_parser("daily")
    _daily.add_argument("--skip-fb", action="store_true",
                        help="skip Facebook Marketplace (needs interactive login)")
    _daily.add_argument("--if-stale", dest="if_stale", action="store_true",
                        help="skip if a scrape already ran today (for the login-triggered job)")
    sub.add_parser("commute").add_argument("--all", action="store_true",
                                           help="recompute all (default: only missing)")

    for cmd in ("view", "interest", "pass", "apply"):
        sub.add_parser(cmd).add_argument("id", type=int)

    _note = sub.add_parser("note")
    _note.add_argument("id", type=int)
    _note.add_argument("text")

    a = sub.add_parser("edit")
    a.add_argument("id", type=int)
    a.add_argument("--title", "-t")
    a.add_argument("--price", "-p", type=int)
    a.add_argument("--location", "-l")

    sub.add_parser("delete").add_argument("id", type=int)

    sub.add_parser("db-status")
    sub.add_parser("db-push").add_argument("--wipe", action="store_true",
                                           help="clear the remote table before pushing")
    sub.add_parser("db-pull").add_argument("--wipe", action="store_true",
                                           help="clear the local table before pulling")

    args = p.parse_args()
    if args.cmd == "fetch":
        args.cmd = "fetch-cl"

    dispatch = {
        "add":        cmd_add,
        "list":       cmd_list,
        "show":       cmd_show,
        "fetch-cl":   cmd_fetch_cl,
        "fetch-fb":   cmd_fetch_fb,
        "fetch-apts": cmd_fetch_apts,
        "fetch-zillow": cmd_fetch_zillow,
        "fetch-rent": cmd_fetch_rent,
        "fetch-hotpads": cmd_fetch_hotpads,
        "enrich-apts": cmd_enrich_apts,
        "prune":      cmd_prune,
        "dedupe":     cmd_dedupe,
        "update":     cmd_update,
        "html":       cmd_html,
        "daily":      cmd_daily,
        "commute":    cmd_commute,
        "view":       lambda a: cmd_set_status(a, "viewed"),
        "interest":   lambda a: cmd_set_status(a, "interested"),
        "pass":       lambda a: cmd_set_status(a, "passed"),
        "apply":      lambda a: cmd_set_status(a, "applied"),
        "note":       cmd_note,
        "edit":       cmd_edit,
        "delete":     cmd_delete,
        "db-status":  cmd_db_status,
        "db-push":    cmd_db_push,
        "db-pull":    cmd_db_pull,
    }
    fn = dispatch.get(args.cmd)
    if fn:
        fn(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()

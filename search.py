#!/usr/bin/env python3
"""
Apartment search tool — Cambridge/Somerville, MA
Searches Craigslist RSS and generates direct links to major listing sites.
"""

import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import json
import sys
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────

CONFIG = {
    "min_price": 2000,
    "max_price": 2800,
    "bedrooms": 1,
    # Scored by commute to Kendall/MIT via Red Line or Green Line Extension
    "target_neighborhoods": [
        {"name": "East Cambridge",    "transit": "10 min walk/bike to Kendall",     "score": 5},
        {"name": "Central Square",    "transit": "Red Line: 1 stop to Kendall",     "score": 5},
        {"name": "Inman Square",      "transit": "Bus/bike ~20 min to Kendall",     "score": 4},
        {"name": "Union Square",      "transit": "Green Line → Kendall ~20 min",    "score": 4},
        {"name": "Cambridgeport",     "transit": "Bike/bus ~15 min to Kendall",     "score": 4},
        {"name": "Porter Square",     "transit": "Red Line: 2 stops to Kendall",    "score": 3},
        {"name": "Davis Square",      "transit": "Red Line: 3 stops to Kendall",    "score": 3},
        {"name": "Harvard Square",    "transit": "Red Line: 2 stops to Kendall",    "score": 3},
        {"name": "Winter Hill",       "transit": "Bus/walk to Davis Red Line",       "score": 2},
        {"name": "Magoun Square",     "transit": "Green Line Ext, transfer needed", "score": 2},
        {"name": "Ball Square",       "transit": "Green Line Ext, transfer needed", "score": 2},
    ],
}

NEIGHBORHOOD_KEYWORDS = [n["name"].lower().split()[0] for n in CONFIG["target_neighborhoods"]]

# ── Craigslist RSS search ────────────────────────────────────────────────────

def search_craigslist(query="cambridge somerville 1 bedroom"):
    base = "https://boston.craigslist.org/search/aap"
    params = {
        "format": "rss",
        "query": query,
        "min_price": CONFIG["min_price"],
        "max_price": CONFIG["max_price"],
        "min_bedrooms": CONFIG["bedrooms"],
        "max_bedrooms": CONFIG["bedrooms"],
        "availabilityMode": 0,
    }
    url = f"{base}?{urllib.parse.urlencode(params)}"
    print(f"  Fetching: {url}\n")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"  [!] Craigslist fetch failed: {e}")
        return []

    # Craigslist RSS uses namespace cl:
    ns = {
        "rss": "http://purl.org/rss/1.0/",
        "cl":  "http://www.craigslist.org/about/cl-rss",
        "dc":  "http://purl.org/dc/elements/1.1/",
    }
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  [!] XML parse error: {e}")
        return []

    items = root.findall(".//item", ns) or root.findall(".//item")
    results = []
    for item in items:
        def t(tag):
            el = item.find(tag, ns) or item.find(tag)
            return el.text.strip() if el is not None and el.text else ""

        title    = t("title")
        link     = t("link")
        price    = t("cl:price") or t("{http://www.craigslist.org/about/cl-rss}price") or extract_price(title)
        location = t("cl:neighborhood") or t("{http://www.craigslist.org/about/cl-rss}neighborhood") or ""
        date     = t("dc:date") or t("{http://purl.org/dc/elements/1.1/}date") or t("pubDate") or ""

        results.append({
            "title":    title,
            "price":    price,
            "location": location,
            "link":     link,
            "date":     date[:10] if len(date) >= 10 else date,
            "score":    neighborhood_score(title + " " + location),
        })

    return sorted(results, key=lambda x: -x["score"])


def extract_price(text):
    import re
    m = re.search(r'\$(\d[\d,]+)', text)
    return f"${m.group(1)}" if m else ""


def neighborhood_score(text):
    text = text.lower()
    for n in CONFIG["target_neighborhoods"]:
        if n["name"].lower() in text:
            return n["score"]
    return 0

# ── Direct search URLs for listing sites ────────────────────────────────────

def build_search_urls():
    mn, mx, br = CONFIG["min_price"], CONFIG["max_price"], CONFIG["bedrooms"]
    return {
        "Craigslist (Cambridge/Somerville)":
            f"https://boston.craigslist.org/search/aap?query=cambridge+somerville"
            f"&min_price={mn}&max_price={mx}&min_bedrooms={br}&max_bedrooms={br}",

        "Apartments.com — Somerville 1BR":
            f"https://www.apartments.com/somerville-ma/1-bedrooms/?max={mx}",

        "Apartments.com — Cambridge 1BR":
            f"https://www.apartments.com/cambridge-ma/1-bedrooms/?max={mx}",

        "Zillow — Cambridge 1BR":
            f"https://www.zillow.com/cambridge-ma/rentals/1-_beds/?price=0%2C{mx}",

        "Zillow — Somerville 1BR":
            f"https://www.zillow.com/somerville-ma/rentals/1-_beds/?price=0%2C{mx}",

        "Redfin — Cambridge rentals":
            f"https://www.redfin.com/city/2833/MA/Cambridge/1-bedroom-apartments-for-rent",

        "Redfin — Somerville rentals":
            f"https://www.redfin.com/city/16064/MA/Somerville/apartments-for-rent",

        "Boston Pads — Cambridge":
            "https://bostonpads.com/cambridge-ma-apartments/",

        "Boston Pads — Somerville":
            "https://bostonpads.com/somerville-ma-apartments/",

        "HotPads — Cambridge/Somerville":
            f"https://hotpads.com/cambridge-ma/apartments-for-rent?minBeds={br}&maxBeds={br}"
            f"&minPrice={mn}&maxPrice={mx}",
    }

# ── Neighborhood guide ───────────────────────────────────────────────────────

NEIGHBORHOOD_GUIDE = """
NEIGHBORHOOD GUIDE — commute to Kendall/MIT (Broad Institute)
═══════════════════════════════════════════════════════════════

★★★★★ BEST BETS (within $2,000–$2,800)

  East Cambridge       Walk/bike to Kendall (~15 min)
                       Avg 1BR: $2,600–$3,200 (high end of budget, check carefully)
                       Pros: zero commute, rapidly improving, lots of new builds
                       Cons: priciest sub-area; some units still industrial-feel

  Central Square       Red Line → Kendall: 1 stop, ~4 min
                       Avg 1BR: $2,400–$2,900 (most in budget)
                       Pros: diverse, great restaurants, on-line, very walkable
                       Cons: can feel grittier than neighboring areas

★★★★  GREAT OPTIONS

  Inman Square         Bus (#CT1, #69) or bike to Kendall ~20 min
                       Avg 1BR: $2,200–$2,700 (well in budget)
                       Pros: quieter, charming, great local spots (Trina's, etc.)
                       Cons: no direct T stop (nearest: Central or Union Sq)

  Union Square         Green Line Ext → Lechmere/Science Park → Kendall ~25 min
  (Somerville)         Avg 1BR: $2,100–$2,600 (budget-friendly)
                       Pros: completely transformed, great food scene, growing
                       Cons: transfer needed for Kendall

  Cambridgeport        Bike to Kendall ~15 min; bus to Central then 1 stop
                       Avg 1BR: $2,200–$2,700
                       Pros: residential, quieter, close to river paths
                       Cons: less vibrant street life

★★★   SOLID CHOICES

  Porter Square        Red Line → Kendall: 2 stops, ~9 min
                       Avg 1BR: $2,200–$2,800
                       Pros: great transit, near Whole Foods, H Mart, Davis energy
                       Cons: slightly farther from Kendall than Central

  Davis Square         Red Line → Kendall: 3 stops, ~14 min
  (Somerville)         Avg 1BR: $2,100–$2,700
                       Pros: best neighborhood vibe in the area, tons of restaurants
                       Cons: 3 stops, prices have risen a lot

  Harvard Square       Red Line → Kendall: 2 stops, ~7 min
                       Avg 1BR: $2,400–$3,000+ (top of/over budget)
                       Pros: iconic, walkable, safe
                       Cons: expensive, touristy

TIPS FOR THIS MARKET
  • Boston's market moves fast — Sept 1 is the big turnover date (students)
  • Off-cycle (Nov–Feb) has less competition and more negotiating room
  • Boston Pads and local brokers (Oxford Street Realty) often have unlisted units
  • Many 1BRs in budget range will be in 3-family houses — check heat/utilities
  • Always ask: what's included? Heat is expensive here in winter
"""

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  APARTMENT SEARCH — Cambridge/Somerville, MA")
    print(f"  Budget: ${CONFIG['min_price']:,}–${CONFIG['max_price']:,}/mo | {CONFIG['bedrooms']}BR")
    print(f"  Commute target: Kendall Square / MIT / Broad Institute")
    print("=" * 60)

    print(NEIGHBORHOOD_GUIDE)

    print("=" * 60)
    print("  SEARCH LINKS (open these in your browser)")
    print("=" * 60)
    for name, url in build_search_urls().items():
        print(f"\n  {name}")
        print(f"  {url}")

    print("\n\n" + "=" * 60)
    print("  LIVE CRAIGSLIST LISTINGS (fetching now...)")
    print("=" * 60 + "\n")

    listings = search_craigslist()

    if not listings:
        print("  No listings returned (Craigslist may have blocked the request).")
        print("  Use the search links above to browse manually.\n")
        return

    for i, apt in enumerate(listings[:20], 1):
        score_stars = "★" * apt["score"] + "☆" * (5 - apt["score"]) if apt["score"] else "  —  "
        print(f"  [{i:02d}] {score_stars}  {apt['price']:>8}  {apt['date']}")
        print(f"       {apt['title'][:65]}")
        if apt["location"]:
            print(f"       📍 {apt['location']}")
        print(f"       {apt['link']}")
        print()

    print(f"  Showing {min(20, len(listings))} of {len(listings)} results.")
    print("  Run with --save to write results to listings.json\n")

    if "--save" in sys.argv:
        with open("listings.json", "w") as f:
            json.dump(listings, f, indent=2)
        print(f"  Saved {len(listings)} listings to listings.json")


if __name__ == "__main__":
    main()

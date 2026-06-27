#!/usr/bin/env python3
"""
Locally hosted apartment-search site.

  python3 server.py            # serves http://localhost:8787 and opens it

Features:
  - Live listing cards from listings.db with status buttons (interested /
    viewed / applied / pass) and notes — clicks update the DB instantly.
  - Client-side filters: house units only, in-unit laundry only, hide passed.
  - Refresh buttons run the scrapers in a background thread:
      * Craigslist  — headless, fully automatic
      * Apartments.com — opens visible Chrome windows (Akamai requires headed)
      * Facebook    — uses the saved .fb_profile login; if not logged in yet,
                      run `python3 track.py fetch-fb` once in a terminal first
  - Binds to 127.0.0.1 only (not reachable from the network).
"""

import json
import threading
import asyncio
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import track

PORT = 8787

# ── Refresh state (single background job at a time) ──────────────────────────

_refresh_lock = threading.Lock()
REFRESH = {"running": False, "source": "", "log": [], "finished": ""}


def _log(msg):
    REFRESH["log"].append(msg)
    print(f"  [refresh] {msg}")


def _run_refresh(source):
    """Background-thread scrape → save. source: cl | apts | fb | all"""
    try:
        conn = track.db_connect()

        if source in ("cl", "all"):
            _log("Craigslist: fetching (headless)...")
            try:
                results = track.scrape_craigslist()
                track._enrich(results)
                track.backfill_listing_dates(conn, results)
                added, skipped = track._save_listings(conn, results, "craigslist")
                track.record_scrape(conn, "craigslist", len(results), len(added))
                _log(f"Craigslist: {len(results)} found, {len(added)} new saved")
            except Exception as e:
                _log(f"Craigslist FAILED: {e}")

        if source in ("apts", "all"):
            _log("Apartments.com: fetching (opens browser windows)...")
            try:
                results = asyncio.run(track._scrape_apts_pw())
                track._enrich(results)
                track.backfill_listing_dates(conn, results)
                added, skipped = track._save_listings(conn, results, "apartments")
                track.record_scrape(conn, "apartments", len(results), len(added))
                if not results:
                    _log("Apartments.com: 0 results — likely Akamai rate-limit; try later")
                else:
                    _log(f"Apartments.com: {len(results)} found, {len(added)} new saved")
            except Exception as e:
                _log(f"Apartments.com FAILED: {e}")

        if source in ("zillow", "all"):
            _log("Zillow: fetching (opens a browser window)...")
            try:
                results = asyncio.run(track._scrape_zillow_pw())
                track._enrich(results)
                track.backfill_listing_dates(conn, results)
                added, skipped = track._save_listings(conn, results, "zillow")
                track.record_scrape(conn, "zillow", len(results), len(added))
                if not results:
                    _log("Zillow: 0 results — if bot-blocked, run "
                         "`python3 track.py fetch-zillow` in a terminal once to solve the captcha")
                else:
                    _log(f"Zillow: {len(results)} found, {len(added)} new saved")
            except Exception as e:
                _log(f"Zillow FAILED: {e}")

        if source in ("rent", "all"):
            _log("Rent.com: fetching (headless)...")
            try:
                results = track.scrape_rent()
                track._enrich(results)
                track.backfill_listing_dates(conn, results)
                added, skipped = track._save_listings(conn, results, "rent")
                track.record_scrape(conn, "rent", len(results), len(added))
                _log(f"Rent.com: {len(results)} found, {len(added)} new saved")
            except Exception as e:
                _log(f"Rent.com FAILED: {e}")

        if source in ("hotpads", "all"):
            _log("HotPads: fetching (opens a browser window)...")
            try:
                results = asyncio.run(track._scrape_hotpads_pw())
                track._enrich(results)
                track.backfill_listing_dates(conn, results)
                added, skipped = track._save_listings(conn, results, "hotpads")
                track.record_scrape(conn, "hotpads", len(results), len(added))
                if not results:
                    _log("HotPads: 0 results — if bot-blocked, run "
                         "`python3 track.py fetch-hotpads` in a terminal once to solve the captcha")
                else:
                    _log(f"HotPads: {len(results)} found, {len(added)} new saved")
            except Exception as e:
                _log(f"HotPads FAILED: {e}")

        if source in ("fb", "all"):
            _log("Facebook: fetching (needs saved login in .fb_profile)...")
            try:
                results = asyncio.run(track._scrape_fb_pw())
                track._enrich(results)
                track.backfill_listing_dates(conn, results)
                added, skipped = track._save_listings(conn, results, "facebook")
                track.record_scrape(conn, "facebook", len(results), len(added))
                if not results:
                    _log("Facebook: 0 results — if not logged in, run "
                         "`python3 track.py fetch-fb` once in a terminal")
                else:
                    _log(f"Facebook: {len(results)} found, {len(added)} new saved")
            except Exception as e:
                _log(f"Facebook FAILED: {e}")

        try:
            track.compute_missing_commutes(conn, log=_log)
        except Exception as e:
            _log(f"Commute computation failed: {e}")

        _log("Done.")
    finally:
        REFRESH["running"] = False
        REFRESH["finished"] = datetime.now().strftime("%H:%M:%S")


# ── Page rendering (reuses track.py template + interactive cards) ────────────

_CONTROLS = """
<div class="controls">
  <div class="filters-dd">
    <button class="btn-filters" onclick="toggleFilters()">⚙ Filters ▾</button>
    <div class="filters-panel" id="filters-panel">
      <div class="fp-row"><label>source</label>
        <select id="f-source" onchange="applyFilters()">
          <option value="all" selected>all</option>
          <option value="craigslist">Craigslist</option>
          <option value="apartments">Apartments.com</option>
          <option value="zillow">Zillow</option>
          <option value="rent">Rent.com</option>
          <option value="hotpads">HotPads</option>
          <option value="facebook">Facebook</option>
        </select></div>
      <div class="fp-row"><label>price /mo</label>
        <input type="number" id="f-pmin" placeholder="min" oninput="applyFilters()" style="width:78px">
        <input type="number" id="f-pmax" placeholder="max" oninput="applyFilters()" style="width:78px"></div>
      <div class="fp-row"><label>min sqft</label>
        <input type="number" id="f-sqft" placeholder="any" oninput="applyFilters()" style="width:78px"></div>
      <div class="fp-row"><label>baths</label>
        <select id="f-baths" onchange="applyFilters()">
          <option value="0">any</option><option value="1">1+</option>
          <option value="1.5">1.5+</option><option value="2">2+</option>
        </select></div>
      <div class="fp-row"><label>max bike</label>
        <select id="f-bike" onchange="applyFilters()">
          <option value="10">10 min</option><option value="15" selected>15 min</option>
          <option value="20">20 min</option><option value="999">any</option>
        </select></div>
      <div class="fp-row"><label>rating</label>
        <label class="cbx"><input type="checkbox" class="f-rate" value="love" onchange="applyFilters()">😍</label>
        <label class="cbx"><input type="checkbox" class="f-rate" value="ok" onchange="applyFilters()">😊</label>
        <label class="cbx"><input type="checkbox" class="f-rate" value="hmm" onchange="applyFilters()">🤔</label>
        <label class="cbx"><input type="checkbox" class="f-rate" value="" onchange="applyFilters()">unrated</label></div>
      <div class="fp-row"><label>amenities</label>
        <label class="cbx"><input type="checkbox" class="f-amen" value="laundry" onchange="applyFilters()">🧺 laundry</label>
        <label class="cbx"><input type="checkbox" class="f-amen" value="parking" onchange="applyFilters()">🚗 parking</label>
        <label class="cbx"><input type="checkbox" class="f-amen" value="wifi" onchange="applyFilters()">📶 wifi</label>
        <label class="cbx"><input type="checkbox" class="f-amen" value="heat" onchange="applyFilters()">🔥 heat</label>
        <label class="cbx"><input type="checkbox" class="f-amen" value="dishwasher" onchange="applyFilters()">🍽 dish</label></div>
      <div class="fp-row">
        <label class="cbx"><input type="checkbox" id="f-house" onchange="applyFilters()"> house only</label>
        <label class="cbx"><input type="checkbox" id="f-hidepassed" checked onchange="applyFilters()"> hide passed</label>
        <label class="cbx"><input type="checkbox" id="f-hideremoved" checked onchange="applyFilters()"> hide removed</label></div>
    </div>
  </div>
  <input type="checkbox" id="f-laundry" style="display:none">
  <span id="f-hidden-count" style="color:#999"></span>
  <button class="btn-swipe" onclick="openSwipe()">▶ Swipe mode</button>
  <span class="spacer"></span>
  <button class="btn-refresh" id="r-cl"   onclick="refresh('cl')">&#8635; Craigslist</button>
  <button class="btn-refresh" id="r-apts" onclick="refresh('apts')">&#8635; Apartments.com</button>
  <button class="btn-refresh" id="r-zillow" onclick="refresh('zillow')">&#8635; Zillow</button>
  <button class="btn-refresh" id="r-rent" onclick="refresh('rent')">&#8635; Rent.com</button>
  <button class="btn-refresh" id="r-hotpads" onclick="refresh('hotpads')">&#8635; HotPads</button>
  <button class="btn-refresh" id="r-fb"   onclick="refresh('fb')">&#8635; Facebook</button>
  <button class="btn-refresh" id="r-all"  onclick="refresh('all')">&#8635; All</button>
</div>
<div id="refresh-log"></div>
<style>
.btn-swipe{background:#4f46e5;color:#fff;border:none;border-radius:8px;padding:6px 14px;font-size:0.9em;font-weight:600;cursor:pointer}
.btn-swipe:hover{background:#4338ca}
.filters-dd{position:relative;display:inline-block}
.btn-filters{background:#fff;border:1px solid #d1d5db;border-radius:8px;padding:6px 14px;font-size:0.9em;font-weight:600;cursor:pointer}
.btn-filters:hover{background:#f3f4f6}
.filters-panel{display:none;position:absolute;top:110%;left:0;z-index:900;background:#fff;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.18);padding:12px 14px;min-width:330px}
.filters-panel.open{display:block}
.fp-row{display:flex;align-items:center;flex-wrap:wrap;gap:8px;padding:5px 0;border-bottom:1px solid #f3f4f6}
.fp-row:last-child{border-bottom:none}
.fp-row>label:first-child{width:74px;font-size:0.8em;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.03em}
.fp-row .cbx{display:inline-flex;align-items:center;gap:3px;font-size:0.9em;font-weight:500;cursor:pointer;width:auto;text-transform:none;letter-spacing:0;color:#374151}
.fp-row input[type=number],.fp-row select{border:1px solid #d1d5db;border-radius:6px;padding:3px 6px;font-size:0.85em}
#swipe-overlay{display:none;position:fixed;inset:0;background:rgba(17,24,39,.92);z-index:1000;
  flex-direction:column;align-items:center;justify-content:flex-start;padding:18px;overflow:auto}
#swipe-overlay.open{display:flex}
.swipe-top{display:flex;justify-content:space-between;align-items:center;width:100%;max-width:560px;color:#e5e7eb;margin-bottom:10px}
.swipe-top .swipe-count{font-weight:700}
.swipe-close{background:transparent;border:1px solid #6b7280;color:#e5e7eb;border-radius:6px;padding:4px 10px;cursor:pointer}
.swipe-stage{width:100%;max-width:600px;display:flex;flex-direction:column;gap:8px}
#swipe-gallery{display:flex;gap:6px;overflow-x:auto;scroll-snap-type:x mandatory;border-radius:12px;background:#000;min-height:60px}
#swipe-gallery img{height:300px;width:auto;flex:0 0 auto;object-fit:cover;scroll-snap-align:center}
#swipe-gallery:empty{display:none}
#swipe-map{width:100%;height:220px;border-radius:12px;overflow:hidden;background:#e8eaed;position:relative}
#swipe-sv{width:100%;height:260px;border-radius:12px;overflow:hidden;background:#111;position:relative}
#swipe-sv img{width:100%;height:100%;object-fit:cover}
#swipe-sv .sv-cap{position:absolute;left:8px;bottom:8px;background:rgba(0,0,0,.6);color:#fff;font-size:.7em;font-weight:700;padding:2px 7px;border-radius:6px}
#swipe-card{width:100%;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.5)}
#swipe-card .media-row{display:none}      /* media shown above via gallery/map/SV */
#swipe-card .rating-col{display:none}      /* rating handled by big buttons below */
#swipe-card .actions,#swipe-card .ac-h{display:none}
#swipe-card .amen-commute .ac-col:last-child{display:none}
.swipe-btns{display:flex;gap:10px;width:100%;max-width:560px;margin-top:14px}
.swipe-btns button{flex:1;border:none;border-radius:10px;padding:14px 0;font-size:1.6em;font-weight:700;cursor:pointer;color:#111}
.sb-hmm{background:#ffff80}
.sb-ok{background:#bfff80}
.sb-love{background:#80ff80}
.sb-skip{background:#6b7280;color:#fff;font-size:1em}.sb-skip:hover{background:#4b5563;flex:0 0 80px}
.swipe-hint{color:#9ca3af;font-size:0.8em;margin-top:10px;text-align:center}
.swipe-cur{color:#fde68a;font-weight:700;margin-left:8px}
</style>
<div id="swipe-overlay">
  <div class="swipe-top">
    <span class="swipe-count" id="swipe-count"></span>
    <button class="swipe-close" onclick="closeSwipe()">✕ close (Esc)</button>
  </div>
  <div class="swipe-stage">
    <div id="swipe-gallery"></div>
    <div id="swipe-map"></div>
    <div id="swipe-sv"></div>
    <div id="swipe-card"></div>
  </div>
  <div class="swipe-btns">
    <button class="sb-hmm"  onclick="swipeRate('hmm')">🤔</button>
    <button class="sb-ok"   onclick="swipeRate('ok')">😊</button>
    <button class="sb-love" onclick="swipeRate('love')">😍</button>
    <button class="sb-skip" onclick="swipeNext()">Skip</button>
  </div>
  <div class="swipe-hint">1 🤔&nbsp;&nbsp;2 😊&nbsp;&nbsp;3 😍&nbsp;&nbsp;&nbsp;space Skip&nbsp;&nbsp;Esc Close</div>
</div>
<script>
async function setRating(id, rating, reload) {
  await fetch('/api/rate', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id:id, rating:rating})});
  // update underlying card in place (no reload needed)
  const card = document.querySelector('.card[data-id="'+id+'"]');
  if (card) {
    card.dataset.rating = rating;
    card.querySelectorAll('.rate').forEach(b => b.classList.remove('rated-on'));
    const map = {no:'.rate-no', hmm:'.rate-hmm', ok:'.rate-ok', love:'.rate-love'};
    const btn = card.querySelector(map[rating]);
    if (btn) btn.classList.add('rated-on');
  }
  if (reload) location.reload();
}
async function setStatus(id, status) {
  await fetch('/api/status', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id:id, status:status})});
  location.reload();
}
async function addNote(id) {
  const text = prompt('Note for listing #' + id + ':');
  if (!text) return;
  await fetch('/api/note', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id:id, text:text})});
  location.reload();
}
async function passHide(id, status) {
  // hide the card immediately, then persist the status (no reload)
  const card = document.querySelector('.card[data-id="'+id+'"]');
  if (card) { card.style.display = 'none'; card.classList.add(status); }
  await fetch('/api/status', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id:id, status:status})});
}
function toggleFilters() {
  document.getElementById('filters-panel').classList.toggle('open');
}
const _val = id => document.getElementById(id).value;
const _checked = sel => Array.from(document.querySelectorAll(sel)).filter(b=>b.checked).map(b=>b.value);
function applyFilters() {
  const houseOnly   = document.getElementById('f-house').checked;
  const hidePassed  = document.getElementById('f-hidepassed').checked;
  const hideRemoved = document.getElementById('f-hideremoved').checked;
  const maxBike     = parseInt(_val('f-bike'), 10);
  const source      = _val('f-source');
  const pmin        = parseFloat(_val('f-pmin')) || 0;
  const pmax        = parseFloat(_val('f-pmax')) || Infinity;
  const minSqft     = parseFloat(_val('f-sqft')) || 0;
  const minBaths    = parseFloat(_val('f-baths')) || 0;
  const wantRatings = _checked('.f-rate');
  const wantAmen    = _checked('.f-amen');
  let hiddenFar = 0;
  document.querySelectorAll('.card').forEach(c => {
    let show = true;
    const bike = parseInt(c.dataset.bike || '998', 10);
    const removed = c.dataset.delisted === '1';
    const price = parseFloat(c.dataset.price) || 0;
    const sqft = parseFloat(c.dataset.sqft) || 0;
    const baths = parseFloat(c.dataset.baths) || 0;
    const rating = c.dataset.rating || '';
    const amen = (c.dataset.amen || '').split(' ');
    if (hideRemoved && removed) show = false;
    if (source !== 'all' && c.dataset.source !== source) show = false;
    if (price && (price < pmin || price > pmax)) show = false;
    if (minSqft && (!sqft || sqft < minSqft)) show = false;
    if (minBaths && (!baths || baths < minBaths)) show = false;
    if (wantRatings.length && !wantRatings.includes(rating)) show = false;
    if (wantAmen.length && !wantAmen.every(a => amen.includes(a))) show = false;
    // bike cap only when not isolating a source; EC never distance-hidden
    if (source === 'all' && bike !== 998 && bike > maxBike && !c.classList.contains('east-cam')) {
      show = false; hiddenFar++;
    }
    if (houseOnly && !c.classList.contains('is-house')) show = false;
    if (hidePassed && (c.classList.contains('passed') || c.classList.contains('gotaway'))) show = false;
    c.style.display = show ? '' : 'none';
  });
  document.getElementById('f-hidden-count').textContent =
    hiddenFar ? hiddenFar + ' hidden (too far)' : '';
  // hide sections that end up empty
  document.querySelectorAll('.section').forEach(s => {
    const cards = s.querySelectorAll('.card');
    if (!cards.length) return;
    const anyVisible = Array.from(cards).some(c => c.style.display !== 'none');
    s.style.display = anyVisible ? '' : 'none';
  });
}
let _poll = null;
async function refresh(source) {
  document.querySelectorAll('.btn-refresh').forEach(b => b.disabled = true);
  await fetch('/api/refresh', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({source:source})});
  _poll = setInterval(pollRefresh, 2000);
}
async function pollRefresh() {
  const r = await (await fetch('/api/refresh-status')).json();
  document.getElementById('refresh-log').textContent = r.log.join('\\n');
  if (!r.running) {
    clearInterval(_poll);
    setTimeout(() => location.reload(), 1200);
  }
}
// ── Swipe mode: one listing at a time, rate with buttons or arrow keys ──
let _swipe = [], _swipeIdx = 0;
function openSwipe() {
  applyFilters();
  _swipe = Array.from(document.querySelectorAll('.card')).filter(c => c.offsetParent !== null);
  if (!_swipe.length) { alert('No listings match the current filters.'); return; }
  _swipeIdx = 0;
  document.getElementById('swipe-overlay').classList.add('open');
  swipeRender();
}
function closeSwipe() {
  document.getElementById('swipe-overlay').classList.remove('open');
  if (_svTimer) { clearInterval(_svTimer); _svTimer = null; }
  if (_swipeMap) { try { _swipeMap.remove(); } catch(e){} _swipeMap = null; }
}
let _svTimer = null, _swipeMap = null;
function swipeRender() {
  const card = _swipe[_swipeIdx];
  document.getElementById('swipe-card').innerHTML = card.outerHTML;
  const rated = card.dataset.rating;
  document.getElementById('swipe-count').innerHTML =
    (_swipeIdx + 1) + ' / ' + _swipe.length +
    (rated ? '<span class="swipe-cur">rated: ' + rated + '</span>' : '');
  // reset media panels, then fill from the media API
  if (_svTimer) { clearInterval(_svTimer); _svTimer = null; }
  if (_swipeMap) { try { _swipeMap.remove(); } catch(e){} _swipeMap = null; }
  document.getElementById('swipe-gallery').innerHTML = '';
  document.getElementById('swipe-map').innerHTML = '';
  document.getElementById('swipe-sv').innerHTML = '';
  const id = parseInt(card.dataset.id, 10);
  fetch('/api/media?id=' + id).then(r => r.json()).then(m => {
    if (parseInt(_swipe[_swipeIdx].dataset.id, 10) !== id) return; // moved on
    swipeGallery(m); swipeMap(m); swipeStreetView(m);
  }).catch(()=>{});
}
function swipeGallery(m) {
  const g = document.getElementById('swipe-gallery');
  (m.photos || []).forEach(u => {
    const img = document.createElement('img');
    img.src = u; img.loading = 'lazy'; img.onerror = () => img.remove();
    g.appendChild(img);
  });
}
function swipeMap(m) {
  if (typeof L === 'undefined' || m.lat == null) return;
  const el = document.getElementById('swipe-map');
  const map = L.map(el, {zoomControl:false, attributionControl:false, dragging:true, scrollWheelZoom:false});
  _swipeMap = map;
  L.tileLayer('https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png', {maxZoom:19, subdomains:'abcd'}).addTo(map);
  const BROAD = [42.36266, -71.08644];
  L.marker([m.lat, m.lon]).addTo(map);
  L.marker(BROAD).bindTooltip('Broad').addTo(map);
  map.fitBounds([[m.lat, m.lon], BROAD], {padding:[40,40], maxZoom:16});
  const R = m.routes || {};
  function line(pts, c) { if (pts && pts.length>1) L.polyline(pts, {color:c, weight:3}).addTo(map); }
  function transit(segs, c) { if (!segs||!segs.length) return; if (Array.isArray(segs[0])) line(segs,c); else segs.forEach(s=>line(s.pts,c)); }
  line(R.walk, '#16a34a');
  // bubbles
  const bb = document.createElement('div'); bb.className = 'map-bubbles';
  let html = '';
  if (m.walk != null) html += '<span class="map-bub bub-walk">🚶 '+m.walk+' min</span>';
  if (m.bike != null) html += '<span class="map-bub bub-bike">🚴 '+m.bike+' min</span>';
  bb.innerHTML = html; el.appendChild(bb);
}
function swipeStreetView(m) {
  if (!m.sv_key || m.sv_lat == null) return;
  const el = document.getElementById('swipe-sv');
  const img = document.createElement('img');
  el.appendChild(img);
  const cap = document.createElement('span'); cap.className='sv-cap'; cap.textContent='📷 Street View'; el.appendChild(cap);
  // 180° sweep centered on the building, in 5° steps (base-90 … base+90)
  const base = (m.sv_heading == null) ? 0 : m.sv_heading;
  const offsets = []; for (let o = -90; o <= 90; o += 10) offsets.push(o);
  const heads = offsets.map(o => (base + o + 360) % 360);
  function url(h) {
    let s = 'https://maps.googleapis.com/maps/api/streetview?size=640x360&location='+m.sv_lat+','+m.sv_lon+
            '&fov=75&source=outdoor&return_error_code=true&key='+m.sv_key;
    if (m.sv_heading != null) s += '&heading='+h;  // omit → API faces the building
    return s;
  }
  heads.forEach(h => { const p = new Image(); p.src = url(h); }); // preload all frames
  // fixed time per frame by section: slightly slower in the middle third,
  // slightly faster through the outer thirds
  const dwell = idx => { const p = idx/(heads.length-1); return (p >= 1/3 && p <= 2/3) ? 300 : 190; };
  let i = (heads.length - 1) >> 1, dir = 1;   // start centered on the building
  img.src = url(heads[i]);
  img.onerror = () => { el.style.display='none'; if (_svTimer) clearTimeout(_svTimer); };
  function step() {
    i += dir;
    if (i >= heads.length) { i = heads.length - 1; dir = -1; }
    else if (i < 0) { i = 0; dir = 1; }
    img.src = url(heads[i]);
    _svTimer = setTimeout(step, dwell(i));
  }
  _svTimer = setTimeout(step, dwell(i));
}
function swipeNext() {
  if (_swipeIdx >= _swipe.length - 1) { closeSwipe(); return; }
  _swipeIdx++; swipeRender();
}
async function swipeRate(rating) {
  const card = _swipe[_swipeIdx];
  if (card) await setRating(parseInt(card.dataset.id, 10), rating, false);
  swipeNext();
}
document.addEventListener('keydown', function(e) {
  if (!document.getElementById('swipe-overlay').classList.contains('open')) return;
  if (e.key === 'Escape') { closeSwipe(); }
  else if (e.key === '1')           { swipeRate('hmm'); }
  else if (e.key === '2')           { swipeRate('ok'); }
  else if (e.key === '3')           { swipeRate('love'); }
  else if (e.key === 'ArrowRight')  { swipeRate('love'); }
  else if (e.key === ' ')           { swipeNext(); }
  else return;
  e.preventDefault();
});
window.addEventListener('DOMContentLoaded', applyFilters);
</script>
"""


_SCRAPE_SOURCES = [
    ("craigslist", "Craigslist"), ("apartments", "Apartments.com"),
    ("zillow", "Zillow"), ("rent", "Rent.com"),
    ("hotpads", "HotPads"), ("facebook", "Facebook"),
]


def _ago(ts):
    """'2026-06-24 21:26' -> '5m ago' / '3h ago' / '2d ago'."""
    if not ts:
        return "never"
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M")
    except ValueError:
        return ts
    secs = (datetime.now() - dt).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _scrape_status_html(conn):
    runs = track.scrape_runs(conn)
    chips = []
    for src, label in _SCRAPE_SOURCES:
        info = runs.get(src) or {}
        ts = info.get("last_run")
        title = ""
        if info.get("last_error"):
            title = f' title="last error: {info["last_error"]}"'
        elif ts:
            title = f' title="{ts} — {info.get("last_total", 0)} found, {info.get("last_new", 0)} new"'
        cls = "scrape-chip" + (" scrape-stale" if not ts else "")
        chips.append(f'<span class="{cls}"{title}><b>{label}</b> {_ago(ts)}</span>')
    return (
        "<style>"
        ".scrape-status{margin:6px 0 2px;font-size:0.8em;color:#555;display:flex;"
        "flex-wrap:wrap;gap:6px;align-items:center}"
        ".scrape-label{font-weight:600;color:#374151}"
        ".scrape-chip{background:#eef2ff;color:#3730a3;border-radius:8px;padding:2px 8px;white-space:nowrap}"
        ".scrape-chip b{font-weight:600}"
        ".scrape-stale{background:#f3f4f6;color:#9ca3af}"
        "</style>"
        '<div class="scrape-status"><span class="scrape-label">Last scraped:</span> '
        + " ".join(chips) + "</div>")


def build_page():
    conn = track.db_connect()
    try:
        track.backfill_derived(conn)  # refresh neighborhood/meta/amenities columns (no network)
    except Exception:
        pass
    rows = conn.execute(
        "SELECT * FROM listings ORDER BY "
        "CASE status WHEN 'interested' THEN 0 WHEN 'applied' THEN 1 WHEN 'new' THEN 2 "
        "WHEN 'viewed' THEN 3 WHEN 'passed' THEN 4 END, id"
    ).fetchall()

    today = datetime.now().strftime("%Y-%m-%d")
    new_ids = {r["id"] for r in rows if (r["added_on"] or "").startswith(today)}
    sections = track._render_sections(rows, new_ids=new_ids, interactive=True)
    if not sections:
        sections = ('<div class="section"><p class="empty">No listings yet — '
                    'hit a refresh button above.</p></div>')

    links = "\n".join(
        f'<a href="{url}" target="_blank">{name}</a>' for name, url in track._SEARCH_LINKS
    )
    hoods = "".join(
        f'<tr><td><strong>{n["name"]}</strong></td>'
        f'<td>{n["transit"]}</td><td>{n["avg"]}</td>'
        f'<td class="score-stars">{"★" * n["score"] + "☆" * (5 - n["score"])}</td></tr>'
        for n in track.NEIGHBORHOODS
    )
    return track._HTML.format(
        date=datetime.now().strftime("%B %d, %Y %H:%M"),
        search_summary=_CONTROLS + _scrape_status_html(conn),
        sections=sections,
        links=links,
        neighborhoods=hoods,
    )


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, build_page())
        elif self.path == "/api/refresh-status":
            self._send(200, json.dumps(REFRESH), "application/json")
        elif self.path.startswith("/api/media"):
            self._send(200, json.dumps(self._media()), "application/json")
        else:
            self._send(404, "not found")

    def _media(self):
        """All photos + map/street-view data for one listing (for swipe mode)."""
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        try:
            lid = int(q.get("id", [""])[0])
        except ValueError:
            return {}
        conn = track.db_connect()
        r = conn.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
        if not r:
            return {}
        def g(k):
            try:
                return r[k]
            except (KeyError, IndexError):
                return None
        routes = {}
        try:
            routes = json.loads(r["route_geo"]) if r["route_geo"] else {}
        except Exception:
            routes = {}
        return {
            "id": lid,
            "photos": track.row_photos(r),
            "street_view": track._streetview_url(r, size="640x360"),
            "sv_lat": g("lat"), "sv_lon": g("lon"),
            "sv_key": track._google_key(),
            "sv_heading": g("sv_heading") if g("sv_heading") is not None
                          else track.streetview_heading(g("lat"), g("lon")),
            "lat": g("lat"), "lon": g("lon"),
            "walk": g("walk_min"), "bike": g("bike_min"),
            "routes": routes,
        }

    def do_POST(self):
        try:
            body = self._json_body()
        except Exception:
            self._send(400, '{"error":"bad json"}', "application/json")
            return

        if self.path == "/api/status":
            lid, status = body.get("id"), body.get("status")
            if status not in track.STATUS_ORDER:
                self._send(400, '{"error":"bad status"}', "application/json")
                return
            conn = track.db_connect()
            conn.execute("UPDATE listings SET status=?, updated_on=? WHERE id=?",
                         (status, track.now(), lid))
            conn.commit()
            self._send(200, '{"ok":true}', "application/json")

        elif self.path == "/api/rate":
            lid, rating = body.get("id"), body.get("rating")
            if rating not in track.RATINGS and rating != "":
                self._send(400, '{"error":"bad rating"}', "application/json")
                return
            conn = track.db_connect()
            conn.execute("UPDATE listings SET rating=?, updated_on=? WHERE id=?",
                         (rating, track.now(), lid))
            conn.commit()
            self._send(200, '{"ok":true}', "application/json")

        elif self.path == "/api/note":
            lid, text = body.get("id"), (body.get("text") or "").strip()
            if not text:
                self._send(400, '{"error":"empty note"}', "application/json")
                return
            conn = track.db_connect()
            r = conn.execute("SELECT notes FROM listings WHERE id=?", (lid,)).fetchone()
            if not r:
                self._send(404, '{"error":"no such listing"}', "application/json")
                return
            notes = ((r["notes"] or "") + f"\n[{track.now()}] {text}").strip()
            conn.execute("UPDATE listings SET notes=?, updated_on=? WHERE id=?",
                         (notes, track.now(), lid))
            conn.commit()
            self._send(200, '{"ok":true}', "application/json")

        elif self.path == "/api/refresh":
            source = body.get("source", "cl")
            if source not in ("cl", "apts", "zillow", "rent", "hotpads", "fb", "all"):
                self._send(400, '{"error":"bad source"}', "application/json")
                return
            with _refresh_lock:
                if REFRESH["running"]:
                    self._send(409, '{"error":"refresh already running"}', "application/json")
                    return
                REFRESH.update({"running": True, "source": source, "log": [], "finished": ""})
            threading.Thread(target=_run_refresh, args=(source,), daemon=True).start()
            self._send(200, '{"ok":true}', "application/json")

        else:
            self._send(404, "not found")

    def log_message(self, fmt, *args):
        pass  # quiet; refresh progress is logged explicitly


def main():
    addr = ("127.0.0.1", PORT)
    httpd = ThreadingHTTPServer(addr, Handler)
    url = f"http://localhost:{PORT}"
    print(f"Apartment search running at {url}  (Ctrl-C to stop)")
    print("Note: Apartments.com/Facebook refreshes open visible browser windows.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

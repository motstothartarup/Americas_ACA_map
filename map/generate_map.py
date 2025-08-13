# map/generate_map.py
# Dots + labels + smooth zoom + simple position DB.
# Stacks appear when zoomed OUT (z <= STACK_ON_AT_Z), grouped by ~30 miles (auto-scaled to px).
# Anchored on one member's label, styled like normal labels.
# Hides ALL labels at very low zoom (z < HIDE_LABELS_BELOW_Z).

import io
import json
import os
import sys
from datetime import datetime, timezone

import folium
import pandas as pd
import requests
from bs4 import BeautifulSoup

# ---------- config ----------
LEVELS = ['Level 1', 'Level 2', 'Level 3', 'Level 3+', 'Level 4', 'Level 4+', 'Level 5']

PALETTE = {
    "Level 1": "#5B2C6F",
    "Level 2": "#00AEEF",
    "Level 3": "#1F77B4",
    "Level 3+": "#2ECC71",
    "Level 4": "#F4D03F",
    "Level 4+": "#E39A33",
    "Level 5": "#E74C3C",
}

RADIUS = {"large": 8, "medium": 7, "small": 6}
STROKE = 2

LABEL_GAP_PX = 10  # vertical gap between dot and label

# --- Zoom tuning knobs (ONLY zoom logic uses these) ---
ZOOM_SNAP = 0.10           # allow fractional zoom
ZOOM_DELTA = 0.25          # keyboard +/- step
WHEEL_PX_PER_ZOOM = 220    # higher = gentler wheel zoom
WHEEL_DEBOUNCE_MS = 15     # smaller = more responsive wheel

# --- Position DB knobs ---
DB_MAX_HISTORY = 200       # keep last N snapshots
UPDATE_DEBOUNCE_MS = 120   # debounce for move/zoom updates

# --- Stacking behavior ---
STACK_ON_AT_Z = 7.5        # stacks when z <= this (zoomed OUT). Tweak (e.g., 8.3)
HIDE_LABELS_BELOW_Z = 5.7  # hide ALL labels when z < this; restore when z >= this

# --- Grouping distance in real-world miles (converted to pixels per zoom) ---
GROUP_RADIUS_MILES = 30.0  # ~30 miles

# --- Visual tweak for stacked rows ---
STACK_ROW_GAP_PX = 6       # spacing between rows in stack (visual)

OUT_DIR = "docs"
OUT_FILE = os.path.join(OUT_DIR, "index.html")


# ---------- helpers ----------
def write_error_page(msg: str) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = """<!doctype html><meta charset="utf-8">
<title>ACA Americas map</title>
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate"/>
<meta http-equiv="Pragma" content="no-cache"/>
<meta http-equiv="Expires" content="0"/>
<style>body{font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;padding:24px;color:#233;max-width:900px;margin:auto;background:#f6f8fb}
.card{background:#fff;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.08);padding:20px}
h1{margin:0 0 10px 0}code{background:#f5f7fb;padding:2px 6px;border-radius:6px}</style>
<div class="card">
  <h1>ACA Americas map</h1>
  <p><strong>Status:</strong> temporarily unavailable.</p>
  <p><strong>Reason:</strong> __MSG__</p>
  <p>Last attempt: __UPDATED__. This page updates automatically once per day.</p>
</div>""".replace("__MSG__", msg).replace("__UPDATED__", updated)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote fallback page:", OUT_FILE)


def fetch_aca_html(timeout: int = 45) -> str:
    url = "https://www.airportcarbonaccreditation.org/accredited-airports/"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ACA-Map-Bot/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def parse_aca_table(html: str) -> pd.DataFrame:
    """Return dataframe with: iata, airport, country, region, aca_level, region4."""
    soup = BeautifulSoup(html, "lxml")
    dfs = []

    table = soup.select_one(".airports-listview table")
    if table is not None:
        try:
            dfs = pd.read_html(io.StringIO(str(table)))
        except Exception as e:
            print("read_html on scoped table failed:", e, file=sys.stderr)

    if not dfs:
        try:
            all_tables = pd.read_html(html)
        except Exception as e:
            raise RuntimeError(f"Could not parse any HTML tables: {e}")
        target = None
        want = {"airport", "airport code", "country", "region", "level"}
        for df in all_tables:
            cols = {str(c).strip().lower() for c in df.columns}
            if want.issubset(cols):
                target = df
                break
        if target is None:
            raise RuntimeError("ACA table not found on the page.")
        dfs = [target]

    raw = dfs[0]
    aca = (
        raw.rename(
            columns={
                "Airport": "airport",
                "Airport code": "iata",
                "Country": "country",
                "Region": "region",
                "Level": "aca_level",
            }
        )[["iata", "airport", "country", "region", "aca_level"]]
    )

    def region4(r: str) -> str:
        if r in ("North America", "Latin America & the Caribbean"):
            return "Americas"
        if r == "UKIMEA":
            return "Europe"
        return r

    aca["region4"] = aca["region"].map(region4)
    aca = aca[aca["aca_level"].isin(LEVELS)].dropna(subset=["iata"])
    if aca.empty:
        raise RuntimeError("ACA dataframe is empty after filtering.")
    return aca


def load_coords() -> pd.DataFrame:
    url = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"
    use = ["iata_code", "latitude_deg", "longitude_deg", "type", "name", "iso_country"]
    df = pd.read_csv(url, usecols=use).rename(columns={"iata_code": "iata"})
    df = df.dropna(subset=["iata", "latitude_deg", "longitude_deg"])
    df["size"] = df["type"].map({"large_airport": "large", "medium_airport": "medium"}).fillna("small")
    return df


# ---------- main ----------
def build_map() -> folium.Map:
    aca_html = fetch_aca_html()
    aca = parse_aca_table(aca_html)
    coords = load_coords()

    amer = (
        aca[aca["region4"].eq("Americas")]
        .merge(coords, on="iata", how="left")
        .dropna(subset=["latitude_deg", "longitude_deg"])
    )
    if amer.empty:
        raise RuntimeError("No rows for the Americas after joining coordinates.")

    bounds = [
        [amer.latitude_deg.min(), amer.longitude_deg.min()],
        [amer.latitude_deg.max(), amer.longitude_deg.max()],
    ]

    m = folium.Map(tiles="CartoDB Positron", zoomControl=True, prefer_canvas=True)
    m.fit_bounds(bounds)
    groups = {lvl: folium.FeatureGroup(name=lvl, show=True).add_to(m) for lvl in LEVELS}

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    BUILD_VER = "base-r1.7-zoom+posdb+stack-out+miles+pane-anchoring"

    # --- CSS + footer badge + zoom meter + stack styles (labels-only look) ---
    badge_html = (
        r"""
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate"/>
<meta http-equiv="Pragma" content="no-cache"/>
<meta http-equiv="Expires" content="0"/>
<style>
.leaflet-tooltip.iata-tt{
  background: transparent; border: 0; box-shadow: none;
  color: #6e6e6e;
  font-family: "Open Sans","Helvetica Neue",Arial,sans-serif;
  font-weight: 1000; font-size: 12px; letter-spacing: 0.5px;
  text-transform: uppercase; white-space: nowrap; text-align:left;
}
.leaflet-tooltip-top:before,
.leaflet-tooltip-bottom:before,
.leaflet-tooltip-left:before,
.leaflet-tooltip-right:before{ display:none !important; }
.leaflet-tooltip.iata-tt .ttxt{ display:inline-block; transform:translate(0px,0px); will-change:transform; }
.leaflet-control-layers-expanded{ box-shadow:0 4px 14px rgba(0,0,0,.12); border-radius:10px; }
.last-updated {
  position:absolute; right:12px; bottom:12px; z-index:9999;
  background:#fff; padding:6px 8px; border-radius:8px;
  box-shadow:0 2px 8px rgba(0,0,0,.12);
  font:12px "Open Sans","Helvetica Neue",Arial,sans-serif; color:#485260;
}
.zoom-meter{
  position:absolute; left:12px; top:12px; z-index:9999;
  background:#fff; padding:6px 8px; border-radius:8px;
  box-shadow:0 2px 8px rgba(0,0,0,.12);
  font:12px "Open Sans","Helvetica Neue",Arial,sans-serif; color:#485260;
  user-select:none; pointer-events:none;
}
/* Stack list styled like labels (no bg/border/shadow) */
.iata-stack{
  position:absolute; z-index:9998; pointer-events:none;
  background:transparent; border:0; box-shadow:none;
  font:12px "Open Sans","Helvetica Neue",Arial,sans-serif;
  color:#6e6e6e; letter-spacing:0.5px; text-transform:uppercase;
  font-weight:1000; text-align:left; white-space:nowrap;
}
.iata-stack .row{ line-height:1.0; margin: __ROWGAP__px 0; }
</style>
<div class="last-updated">Last updated: __UPDATED__ • __VER__</div>
<div id="zoomMeter" class="zoom-meter">Zoom: --%</div>
"""
        .replace("__UPDATED__", updated)
        .replace("__VER__", BUILD_VER)
        .replace("__ROWGAP__", str(int(STACK_ROW_GAP_PX)))
    )
    m.get_root().html.add_child(folium.Element(badge_html))

    # --- dots + permanent tooltips (labels) ---
    for _, r in amer.iterrows():
        lat, lon = float(r.latitude_deg), float(r.longitude_deg)
        size = r.size
        radius = RADIUS.get(size, 6)
        offset_y = -(radius + STROKE + max(LABEL_GAP_PX, 1))

        dot = folium.CircleMarker(
            [lat, lon],
            radius=radius,
            color="#111",
            weight=STROKE,
            fill=True,
            fill_color=PALETTE.get(r.aca_level, "#666"),
            fill_opacity=0.95,
            popup=folium.Popup(
                "<b>{airport}</b><br>IATA: {iata}<br>ACA: <b>{lvl}</b><br>Country: {ctry}".format(
                    airport=r.airport, iata=r.iata, lvl=r.aca_level, ctry=r.country
                ),
                max_width=320,
            ),
        )
        dot.add_child(
            folium.Tooltip(
                text=r.iata,
                permanent=True,
                direction="top",
                offset=(0, offset_y),
                sticky=False,
                class_name="iata-tt size-{size} tt-{iata}".format(size=size, iata=r.iata),
            )
        )
        dot.add_to(groups[r.aca_level])

    folium.LayerControl(collapsed=False).add_to(m)

    # --- JS: smooth zoom + zoom meter + position DB + stacks on zoom-out + miles->px scaling ---
    js = r"""
(function(){
  try {
    const MAP_NAME = "__MAP_NAME__";
    const ZOOM_SNAP = __ZOOM_SNAP__;
    const ZOOM_DELTA = __ZOOM_DELTA__;
    const WHEEL_PX = __WHEEL_PX__;
    const WHEEL_DEBOUNCE = __WHEEL_DEBOUNCE__;
    const DB_MAX_HISTORY = __DB_MAX_HISTORY__;
    const UPDATE_DEBOUNCE_MS = __UPDATE_DEBOUNCE_MS__;

    // behavior
    const STACK_ON_AT_Z = __STACK_ON_AT_Z__;              // stacks when z <= this
    const HIDE_LABELS_BELOW_Z = __HIDE_LABELS_BELOW_Z__;  // hide all labels when z < this
    const GROUP_RADIUS_MILES = __GROUP_RADIUS_MILES__;     // miles, scaled to px per zoom

    // snapshot DB
    window.ACA_DB = window.ACA_DB || { latest:null, history:[] };
    function pushSnapshot(snap){
      window.ACA_DB.latest = snap;
      window.ACA_DB.history.push(snap);
      if (window.ACA_DB.history.length > DB_MAX_HISTORY){
        window.ACA_DB.history.splice(0, window.ACA_DB.history.length - DB_MAX_HISTORY);
      }
    }
    window.ACA_DB.get = function(){ return window.ACA_DB.latest; };
    window.ACA_DB.export = function(){ try { return JSON.stringify(window.ACA_DB.latest, null, 2); } catch(e){ return "{}"; } };

    function until(cond, cb, tries=200, delay=50){
      (function tick(n){ if(cond()) return cb(); if(n<=0) return; setTimeout(()=>tick(n-1), delay); })(tries);
    }

    until(()=> typeof window[MAP_NAME] !== "undefined" &&
             window[MAP_NAME] &&
             window[MAP_NAME].getPanes &&
             window[MAP_NAME].getContainer,
          init, 200, 50);

    function init(){
      const map  = window[MAP_NAME];
      const pane = map.getPanes().tooltipPane;

      // smooth wheel zoom
      function tuneWheel(){
        map.options.zoomSnap = ZOOM_SNAP;
        map.options.zoomDelta = ZOOM_DELTA;
        map.options.wheelPxPerZoomLevel = WHEEL_PX;
        map.options.wheelDebounceTime   = WHEEL_DEBOUNCE;
        if (map.scrollWheelZoom){ map.scrollWheelZoom.disable(); map.scrollWheelZoom.enable(); }
      }
      tuneWheel();

      // zoom meter
      const meter = document.getElementById('zoomMeter');
      function updateMeter(){
        if (!meter) return;
        const z = map.getZoom();
        const minZ = (map.getMinZoom && map.getMinZoom()) || 0;
        let maxZ = (map.getMaxZoom && map.getMaxZoom()); if (maxZ == null) maxZ = 19;
        const pct = Math.round(((z - minZ)/Math.max(1e-6, (maxZ - minZ))) * 100);
        meter.textContent = "Zoom: " + pct + "% (z=" + z.toFixed(2) + ")";
      }

      // helpers
      function rectBaseForPane(thePane){
        // compute rectangles relative to the *pane*, not the container
        const prect = thePane.getBoundingClientRect();
        return function rect(el){
          const r = el.getBoundingClientRect();
          return { x:r.left - prect.left, y:r.top - prect.top, w:r.width, h:r.height };
        };
      }
      function ensureWrap(el){
        let txt = el.querySelector('.ttxt');
        if (!txt){
          const span = document.createElement('span');
          span.className = 'ttxt';
          span.textContent = el.textContent;
          el.textContent = '';
          el.appendChild(span);
          txt = span;
        }
        return txt;
      }
      function showAllLabels(){
        pane.querySelectorAll('.iata-tt').forEach(el => { el.style.display = ''; });
      }
      function hideAllLabels(){
        pane.querySelectorAll('.iata-tt').forEach(el => { el.style.display = 'none'; });
      }
      function clearStacks(){
        pane.querySelectorAll('.iata-stack').forEach(n => n.remove());
      }

      // miles -> pixels at current zoom (approx at map center, horizontal)
      function milesToPixels(miles){
        const meters = miles * 1609.344;
        const center = map.getCenter();
        const p1 = map.latLngToContainerPoint(center);
        const p2 = L.point(p1.x + 100, p1.y);
        const ll2 = map.containerPointToLatLng(p2);
        const metersPer100px = map.distance(center, ll2);
        const pxPerMeter = 100 / Math.max(1e-6, metersPer100px);
        return meters * pxPerMeter;
      }

      // collect label/dot items (measure relative to the tooltip pane)
      function collectItems(){
        const rect = rectBaseForPane(pane);
        const items = [];
        map.eachLayer(lyr=>{
          if (!(lyr instanceof L.CircleMarker)) return;
          const tt = (lyr.getTooltip && lyr.getTooltip()) || null;
          if (!tt) return;
          if (!tt._container) tt.update();
          const el = tt._container;
          if (!el || !el.classList.contains('iata-tt')) return;
          el.style.display = ''; // ensure visible to measure

          const latlng = lyr.getLatLng();
          const pt = map.latLngToContainerPoint(latlng); // for clustering distance only
          const cls = Array.from(el.classList);
          const size = (cls.find(c=>c.startsWith('size-'))||'size-small').slice(5);
          const iata = (cls.find(c=>c.startsWith('tt-'))||'tt-').slice(3);
          const color = (lyr.options && (lyr.options.fillColor || lyr.options.color)) || "#666";

          const txt = ensureWrap(el);
          const R = rect(txt); // coords relative to the pane
          const cx = R.x + R.w/2, cy = R.y + R.h/2;

          items.push({
            iata, size, color, el,
            dot:   { lat: latlng.lat, lng: latlng.lng, x: pt.x, y: pt.y },
            label: { x: R.x, y: R.y, w: R.w, h: R.h, cx, cy }
          });
        });
        return items;
      }

      // cluster by screen pixels (union-find), radius derived from miles
      function buildClusters(items, radiusPx){
        const n = items.length;
        const parent = Array.from({length:n}, (_,i)=>i);
        function find(a){ return parent[a]===a ? a : (parent[a]=find(parent[a])); }
        function uni(a,b){ a=find(a); b=find(b); if(a!==b) parent[b]=a; }
        const R2 = radiusPx * radiusPx;
        for (let i=0;i<n;i++){
          for (let j=i+1;j<n;j++){
            const dx = items[i].dot.x - items[j].dot.x;
            const dy = items[i].dot.y - items[j].dot.y;
            if (dx*dx + dy*dy <= R2) uni(i,j);
          }
        }
        const groups = new Map();
        for (let i=0;i<n;i++){
          const r = find(i);
          if (!groups.has(r)) groups.set(r, []);
          groups.get(r).push(i);
        }
        return Array.from(groups.values()).filter(g => g.length >= 2);
      }

      // draw stack anchored at one member's label (topmost by y)
      function drawStack(groupIdxs, items){
        const div = document.createElement('div');
        div.className = 'iata-stack';

        // anchor = topmost label in the group
        const sorted = groupIdxs.slice().sort((a,b)=> items[a].label.y - items[b].label.y);
        const anchorIdx = sorted[0];
        const anchor = items[anchorIdx];

        // rows: plain text like labels
        sorted.forEach(i=>{
          const r = document.createElement('div');
          r.className = 'row';
          r.textContent = items[i].iata;
          div.appendChild(r);
        });
        pane.appendChild(div);

        // place exactly where the anchor label is (top-left), after layout settles
        requestAnimationFrame(()=>{
          div.style.left = Math.round(anchor.label.x) + "px";
          div.style.top  = Math.round(anchor.label.y) + "px";
        });

        return {
          anchor: { iata: anchor.iata, x: anchor.label.x, y: anchor.label.y },
          iatas: sorted.map(i=>items[i].iata)
        };
      }

      function applyClustering(items){
        clearStacks();
        showAllLabels();

        const z = map.getZoom();

        // global hide at very low zoom
        if (z < HIDE_LABELS_BELOW_Z){
          hideAllLabels();
          return { stacks: [], hidden: [], hiddenAll:true };
        }

        // only stack when zoomed OUT enough
        if (z > STACK_ON_AT_Z) return { stacks: [], hidden: [], hiddenAll:false };

        const radiusPx = milesToPixels(GROUP_RADIUS_MILES);  // scales with zoom
        const clusters = buildClusters(items, radiusPx);
        const hidden = [];
        const stacks = [];
        clusters.forEach(g=>{
          // hide member labels
          g.forEach(i=>{ items[i].el.style.display = 'none'; hidden.push(items[i].iata); });
          // draw stack anchored at a member
          stacks.push(drawStack(g, items));
        });
        return { stacks, hidden, hiddenAll:false };
      }

      function buildSnapshot(items, stacks){
        const now = new Date().toISOString();
        const z = map.getZoom();
        const b = map.getBounds();
        return {
          ts: now,
          zoom: z,
          bounds: { n: b.getNorth(), s: b.getSouth(), e: b.getEast(), w: b.getWest() },
          size: { w: map.getSize().x, h: map.getSize().y },
          count: items.length,
          items: items.map(it=>({
            iata: it.iata, size: it.size, color: it.color,
            dot: it.dot, label: it.label
          })),
          stacks
        };
      }

      // --- update cycle (after zoom settles) ---
      let tmr = null;
      function updateAll(){
        updateMeter();
        // Double RAF: wait for Leaflet to finish placing layers/labels after zoom.
        requestAnimationFrame(()=>requestAnimationFrame(()=>{
          const items = collectItems();              // positions relative to pane
          const { stacks } = applyClustering(items); // cluster + draw
          pushSnapshot(buildSnapshot(items, stacks));
        }));
      }
      function scheduleUpdate(){
        if (tmr) clearTimeout(tmr);
        tmr = setTimeout(updateAll, UPDATE_DEBOUNCE_MS);
      }

      if (map.whenReady) map.whenReady(updateAll);
      updateMeter();

      // Keep meter snappy while zooming
      map.on('zoom', updateMeter);

      // Compute positions/clusters only when motion/zoom finishes
      map.on('zoomend moveend overlayadd overlayremove layeradd layerremove resize', scheduleUpdate);

      const snap = window.ACA_DB.get();
      if (snap){
        console.debug("[ACA] snapshot @", snap.ts, "items:", snap.count, "stacks:", (snap.stacks||[]).length);
      }
    }
  } catch (err) {
    console.error("[ACA] init failed:", err);
  }
})();
"""

    js = (js
          .replace("__MAP_NAME__", m.get_name())
          .replace("__ZOOM_SNAP__", str(float(ZOOM_SNAP)))
          .replace("__ZOOM_DELTA__", str(float(ZOOM_DELTA)))
          .replace("__WHEEL_PX__", str(int(WHEEL_PX_PER_ZOOM)))
          .replace("__WHEEL_DEBOUNCE__", str(int(WHEEL_DEBOUNCE_MS)))
          .replace("__DB_MAX_HISTORY__", str(int(DB_MAX_HISTORY)))
          .replace("__UPDATE_DEBOUNCE_MS__", str(int(UPDATE_DEBOUNCE_MS)))
          .replace("__STACK_ON_AT_Z__", str(float(STACK_ON_AT_Z)))
          .replace("__HIDE_LABELS_BELOW_Z__", str(float(HIDE_LABELS_BELOW_Z)))
          .replace("__GROUP_RADIUS_MILES__", str(float(GROUP_RADIUS_MILES)))
    )

    m.get_root().script.add_child(folium.Element(js))
    return m


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        fmap = build_map()
        fmap.save(OUT_FILE)
        print("Wrote", OUT_FILE)
    except Exception as e:
        print("ERROR building map:", e, file=sys.stderr)
        write_error_page(str(e))
        sys.exit(0)

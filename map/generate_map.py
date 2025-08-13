# map/generate_map.py
# Dots + labels + smooth zoom + simple position DB.
# NEW: thresholded cluster stacks (pixel-based) at high zoom.

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

# --- Simple cluster stacker knobs (screen-pixel based) ---
STACK_ON_AT_Z = 8.3        # start stacking at/above this Leaflet zoom
CLUSTER_R_PX = 120         # proximity radius in screen pixels (≈ your “60mi”—tune freely)
STACK_LIST_OFFSET_PX = 10  # vertical gap above cluster center
STACK_ROW_GAP_PX = 6       # spacing between rows in stack

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
    BUILD_VER = "base-r1.1-zoom+posdb+stack"

    # --- CSS + footer badge + zoom meter + stack styles ---
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
/* simple stack list (left-justified text) */
.iata-stack{
  position:absolute; z-index:9998; pointer-events:none;
  background:#fff; color:#485260; text-align:left;
  border:1px solid rgba(0,0,0,.25); border-radius:8px;
  padding:6px 8px; box-shadow:0 2px 12px rgba(0,0,0,.14);
  font:12px "Open Sans","Helvetica Neue",Arial,sans-serif;
}
.iata-stack .row{ line-height:1.30; white-space:nowrap; margin: __ROWGAP__px 0; }
.iata-stack .dot{
  display:inline-block; width:6px; height:6px; border-radius:50%;
  margin-right:6px; border:1px solid rgba(0,0,0,.25);
  transform: translateY(-1px);
}
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

    # --- JS: smooth zoom + zoom meter + position DB + simple pixel-based stacks ---
    js = r"""
(function(){
  try {
    // Map + knobs
    const MAP_NAME = "__MAP_NAME__";
    const ZOOM_SNAP = __ZOOM_SNAP__;
    const ZOOM_DELTA = __ZOOM_DELTA__;
    const WHEEL_PX = __WHEEL_PX__;
    const WHEEL_DEBOUNCE = __WHEEL_DEBOUNCE__;
    const DB_MAX_HISTORY = __DB_MAX_HISTORY__;
    const UPDATE_DEBOUNCE_MS = __UPDATE_DEBOUNCE_MS__;

    const STACK_ON_AT_Z = __STACK_ON_AT_Z__;
    const CLUSTER_R_PX  = __CLUSTER_R_PX__;
    const STACK_LIST_OFFSET_PX = __STACK_LIST_OFFSET_PX__;

    // Small DB for snapshots
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
      (function tick(n){
        if (cond()) return cb();
        if (n<=0) return;
        setTimeout(()=>tick(n-1), delay);
      })(tries);
    }

    until(
      ()=> typeof window[MAP_NAME] !== "undefined" &&
          window[MAP_NAME] &&
          window[MAP_NAME].getPanes &&
          window[MAP_NAME].getContainer,
      init,
      200, 50
    );

    function init(){
      const map = window[MAP_NAME];
      const pane = map.getPanes().tooltipPane;

      // ---- Smooth, fine-grained wheel zoom ----
      function tuneWheel(){
        map.options.zoomSnap = ZOOM_SNAP;
        map.options.zoomDelta = ZOOM_DELTA;
        map.options.wheelPxPerZoomLevel = WHEEL_PX;
        map.options.wheelDebounceTime = WHEEL_DEBOUNCE;
        if (map.scrollWheelZoom){
          map.scrollWheelZoom.disable();
          map.scrollWheelZoom.enable();
        }
      }
      tuneWheel();

      // ---- Zoom meter ----
      const meter = document.getElementById('zoomMeter');
      function updateMeter(){
        if (!meter) return;
        const z = map.getZoom();
        const minZ = (typeof map.getMinZoom === 'function' && map.getMinZoom()) || 0;
        let maxZ = (typeof map.getMaxZoom === 'function' && map.getMaxZoom());
        if (maxZ == null) maxZ = 19;
        const pct = Math.round( ( (z - minZ) / Math.max(1e-6, (maxZ - minZ)) ) * 100 );
        meter.textContent = "Zoom: " + pct + "% (z=" + z.toFixed(2) + ")";
      }

      // ---- Geometry helpers ----
      function getContainer(){ return map.getContainer(); }
      function rectBase(){
        const crect = getContainer().getBoundingClientRect();
        return function rect(el){
          const r = el.getBoundingClientRect();
          return { x: r.left - crect.left, y: r.top - crect.top, w: r.width, h: r.height };
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
        if (!pane) return;
        pane.querySelectorAll('.iata-tt').forEach(el=>{ el.style.display = ''; });
      }
      function clearStacks(){
        if (!pane) return;
        pane.querySelectorAll('.iata-stack').forEach(n => n.remove());
      }

      // ---- Collect all items (labels are made visible for measurement) ----
      function collectItems(){
        const rect = rectBase();
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
          const pt = map.latLngToContainerPoint(latlng);
          const cls = Array.from(el.classList);
          const size = (cls.find(c=>c.startsWith('size-'))||'size-small').slice(5);
          const iata = (cls.find(c=>c.startsWith('tt-'))||'tt-').slice(3);
          const color = (lyr.options && (lyr.options.fillColor || lyr.options.color)) || "#666";

          const txt = ensureWrap(el);
          const R = rect(txt);
          const cx = R.x + R.w/2, cy = R.y + R.h/2;

          items.push({
            iata, size, color, el,
            dot:   { lat: latlng.lat, lng: latlng.lng, x: pt.x, y: pt.y },
            label: { x: R.x, y: R.y, w: R.w, h: R.h, cx, cy }
          });
        });
        return items;
      }

      // ---- Build pixel clusters (union-find) ----
      function buildClusters(items){
        const n = items.length;
        const parent = Array.from({length:n}, (_,i)=>i);
        function find(a){ return parent[a]===a ? a : (parent[a]=find(parent[a])); }
        function uni(a,b){ a=find(a); b=find(b); if(a!==b) parent[b]=a; }
        const R2 = CLUSTER_R_PX * CLUSTER_R_PX;

        for(let i=0;i<n;i++){
          for(let j=i+1;j<n;j++){
            const dx = items[i].dot.x - items[j].dot.x;
            const dy = items[i].dot.y - items[j].dot.y;
            if (dx*dx + dy*dy <= R2) uni(i,j);
          }
        }
        const groups = new Map();
        for(let i=0;i<n;i++){
          const r = find(i);
          if(!groups.has(r)) groups.set(r, []);
          groups.get(r).push(i);
        }
        return Array.from(groups.values()).filter(g => g.length >= 2);
      }

      // ---- Draw a left-justified stack centered horizontally at cluster center, above by offset ----
      function drawStack(groupIdxs, items){
        const div = document.createElement('div');
        div.className = 'iata-stack';

        // sort rows by y to get a stable list
        const rows = groupIdxs.slice().sort((a,b)=> items[a].label.y - items[b].label.y);
        rows.forEach(i=>{
          const r = document.createElement('div');
          r.className = 'row';
          const dot = document.createElement('span');
          dot.className = 'dot';
          dot.style.background = items[i].color;
          const t = document.createElement('span');
          t.textContent = items[i].iata;
          r.appendChild(dot); r.appendChild(t);
          div.appendChild(r);
        });
        pane.appendChild(div);

        // centroid of dot screen points
        let cx=0, cy=0;
        groupIdxs.forEach(i=>{ cx += items[i].dot.x; cy += items[i].dot.y; });
        cx /= groupIdxs.length; cy /= groupIdxs.length;

        const w = div.getBoundingClientRect().width;
        const h = div.getBoundingClientRect().height;

        const left = Math.round(cx - w/2);
        const top  = Math.round(cy - h - STACK_LIST_OFFSET_PX);

        div.style.left = left + "px";
        div.style.top  = top  + "px";

        return {
          center: { x: cx, y: cy },
          box: { left, top, width: w, height: h },
          iatas: groupIdxs.map(i=>items[i].iata)
        };
      }

      // ---- Apply (or clear) clustering based on zoom ----
      function applyClustering(items){
        clearStacks();
        showAllLabels();

        const z = map.getZoom();
        if (z < STACK_ON_AT_Z) return { stacks: [], hidden: [] };

        const clusters = buildClusters(items);
        const hidden = [];
        const stacks = [];

        clusters.forEach(g=>{
          // hide the individual labels for members of this cluster
          g.forEach(i=>{ items[i].el.style.display = 'none'; hidden.push(items[i].iata); });
          // draw stack above the cluster center
          stacks.push(drawStack(g, items));
        });

        return { stacks, hidden };
      }

      // ---- Snapshot builder ----
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
          stacks // [{center:{x,y}, box:{left,top,width,height}, iatas:[...]}]
        };
      }

      // ---- Debounced updater on pan/zoom ----
      let tmr = null;
      function updateAll(){
        updateMeter();
        const items = collectItems();
        const { stacks } = applyClustering(items);
        pushSnapshot(buildSnapshot(items, stacks));
      }
      function scheduleUpdate(){
        if (tmr) clearTimeout(tmr);
        tmr = setTimeout(updateAll, UPDATE_DEBOUNCE_MS);
      }

      // initial + events
      if (map.whenReady) map.whenReady(updateAll);
      updateMeter(); // in case already ready

      map.on('zoom zoomend', updateMeter);
      map.on('move moveend zoom zoomend overlayadd overlayremove layeradd layerremove', scheduleUpdate);

      // Console hint
      const snap = window.ACA_DB.get();
      if (snap){
        console.debug("[ACA] snapshot @", snap.ts, "items:", snap.count, "stacks:", (snap.stacks||[]).length);
        console.debug("[ACA] try: window.ACA_DB.get(), window.ACA_DB.export()");
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
          .replace("__CLUSTER_R_PX__", str(int(CLUSTER_R_PX)))
          .replace("__STACK_LIST_OFFSET_PX__", str(int(STACK_LIST_OFFSET_PX)))
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

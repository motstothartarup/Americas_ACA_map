# map/generate_map.py
# Builds docs/index.html with the ACA Americas map (dots + labels only),
# smooth/fine-grained zoom, and a simple JS "database" of on-screen positions
# of dots and label centers that updates on load and after pan/zoom.

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
    BUILD_VER = "base-r1.0-zoom+posdb"

    # --- CSS + footer badge + zoom meter ---
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
  text-transform: uppercase; white-space: nowrap;
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
</style>
<div class="last-updated">Last updated: __UPDATED__ • __VER__</div>
<div id="zoomMeter" class="zoom-meter">Zoom: --%</div>
"""
        .replace("__UPDATED__", updated)
        .replace("__VER__", BUILD_VER)
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

    # --- JS: smooth zoom + zoom meter + simple position DB (no label organizing) ---
    js = r"""
(function(){
  try {
    // Look up the map variable *by name* from window to avoid ReferenceError
    const MAP_NAME = "__MAP_NAME__";
    const ZOOM_SNAP = __ZOOM_SNAP__;
    const ZOOM_DELTA = __ZOOM_DELTA__;
    const WHEEL_PX = __WHEEL_PX__;
    const WHEEL_DEBOUNCE = __WHEEL_DEBOUNCE__;
    const DB_MAX_HISTORY = __DB_MAX_HISTORY__;
    const UPDATE_DEBOUNCE_MS = __UPDATE_DEBOUNCE_MS__;

    // Tiny DB for positions
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

      // ---- Helpers for measuring on-screen positions ----
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

      // ---- Collect positions snapshot ----
      function collectPositions(){
        const rect = rectBase();
        const items = [];
        map.eachLayer(lyr=>{
          if (!(lyr instanceof L.CircleMarker)) return;
          const tt = (lyr.getTooltip && lyr.getTooltip()) || null;
          if (!tt) return;
          if (!tt._container) tt.update();
          const el = tt._container;
          if (!el || !el.classList.contains('iata-tt')) return;

          const latlng = lyr.getLatLng();
          const dotPt = map.latLngToContainerPoint(latlng);
          const cls = Array.from(el.classList);
          const size = (cls.find(c=>c.startsWith('size-'))||'size-small').slice(5);
          const iata = (cls.find(c=>c.startsWith('tt-'))||'tt-').slice(3);
          const color = (lyr.options && (lyr.options.fillColor || lyr.options.color)) || "#666";

          const txt = ensureWrap(el);
          const R = rect(txt);
          const cx = R.x + R.w/2, cy = R.y + R.h/2;

          items.push({
            iata, size, color,
            dot:   { lat: latlng.lat, lng: latlng.lng, x: dotPt.x, y: dotPt.y },
            label: { x: R.x, y: R.y, w: R.w, h: R.h, cx, cy }
          });
        });

        const now = new Date().toISOString();
        const z = map.getZoom();
        const b = map.getBounds();
        const snap = {
          ts: now,
          zoom: z,
          bounds: { n: b.getNorth(), s: b.getSouth(), e: b.getEast(), w: b.getWest() },
          size: { w: map.getSize().x, h: map.getSize().y },
          count: items.length,
          items
        };
        pushSnapshot(snap);
        return snap;
      }

      // ---- Debounced updater on pan/zoom ----
      let tmr = null;
      function scheduleUpdate(){
        if (tmr) clearTimeout(tmr);
        tmr = setTimeout(function(){
          updateMeter();
          collectPositions();
        }, UPDATE_DEBOUNCE_MS);
      }

      // initial + events
      if (map.whenReady) map.whenReady(function(){
        updateMeter();
        collectPositions();
      });
      updateMeter(); // also call once in case map is already ready

      map.on('zoom zoomend', updateMeter);
      map.on('move moveend zoom zoomend overlayadd overlayremove layeradd layerremove', scheduleUpdate);

      // Console hint
      const snap = window.ACA_DB.get();
      if (snap){
        console.debug("[ACA] positions snapshot @", snap.ts, "items:", snap.count);
        console.debug("[ACA] try: window.ACA_DB.get(), window.ACA_DB.export()");
      }
    }
  } catch (err) {
    console.error("[ACA] init failed:", err);
  }
})();
"""


    js = (js
          .replace("__MAP__", m.get_name())
          .replace("__ZOOM_SNAP__", str(float(ZOOM_SNAP)))
          .replace("__ZOOM_DELTA__", str(float(ZOOM_DELTA)))
          .replace("__WHEEL_PX__", str(int(WHEEL_PX_PER_ZOOM)))
          .replace("__WHEEL_DEBOUNCE__", str(int(WHEEL_DEBOUNCE_MS)))
          .replace("__DB_MAX_HISTORY__", str(int(DB_MAX_HISTORY)))
          .replace("__UPDATE_DEBOUNCE_MS__", str(int(UPDATE_DEBOUNCE_MS)))
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

# map/generate_map.py
# Builds docs/index.html with the ACA Americas map (labels only, smart placement near dots).
# If data fetch fails, writes a fallback page so Pages still serves something.

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

LABEL_GAP_PX = 10
SHOW_AT = dict(large=2, medium=3, small=4)   # base gates; ~20% extension at runtime
PAD_PX = 4                                    # ↑ makes overlap detection a bit more sensitive

# Placement solver knobs
DRIFT_PX = 18     # max label drift from dot (px)
ITERS = 16        # relaxation iterations
FSTEP = 0.50      # solver step

EXT_FRACTION = 0.20  # ~20% of zoom range for “extended keep”

# --- NEW behavior knobs (you can tweak these) ---
DOT_HIDE_BELOW_Z = 4         # hide all DOTS (not labels) when zoom < this
ALIGN_LEFT_AT_Z   = 6        # when zoom <= this, stacks anchor LEFT of cluster bbox
STACK_R_PX        = 52       # screen-pixel proximity that triggers stacking
NEAR_MILES        = 50       # “same city” clustering radius (miles)
LIST_OFFSET_PX    = 12       # gap between cluster bbox and stacked list
STACK_ROW_GAP_PX  = 4        # extra space between rows in a stack

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
    SOLVER_VER = "solver-r3.7-stacks+city+hide"

    # --- CSS + footer badge (tokens replaced inline) ---
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
/* stacked cluster label */
.iata-stack{
  position:absolute; z-index:9998; pointer-events:none;
  background:#fff; color:#485260;
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
"""
        .replace("__UPDATED__", updated)
        .replace("__VER__", SOLVER_VER)
        .replace("__ROWGAP__", str(int(STACK_ROW_GAP_PX)))
    )
    m.get_root().html.add_child(folium.Element(badge_html))

    # dots + permanent tooltips
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

    # --- JS: relaxation + stacked lists + city clustering + dot hiding (no <script> tags) ---
    js = r"""
(function(){
  try {
    console.debug("[ACA] solver r3.7-stacks+city+hide start");

    const MAP = __MAP__;
    const SHOWZ = __SHOWZ__;
    const PAD   = __PAD__;
    const PRIOR = {large:0, medium:1, small:2};
    const DRIFT = __DRIFT__;
    const EXTFRAC = __EXTFRAC__;
    const ITERS = __ITERS__;
    const FSTEP = __FSTEP__;

    // behavior/stack knobs
    const DOT_HIDE_Z   = __DOT_HIDE_Z__;
    const ALIGN_LEFT_Z = __ALIGN_LEFT_Z__;
    const STACK_R      = __STACK_R__;
    const LIST_OFFSET  = __LIST_OFFSET__;
    const NEAR_MILES   = __NEAR_MILES__;
    const ROW_GAP      = __ROW_GAP__;

    function until(cond, cb, tries=80, delay=100){
      (function tick(n){
        if (cond()) return cb();
        if (n<=0) return;
        setTimeout(()=>tick(n-1), delay);
      })(tries);
    }

    until(
      ()=> typeof MAP !== "undefined" && MAP && MAP.getPanes && MAP.getContainer,
      init,
      80, 100
    );

    function init(){
      const map = MAP;
      console.debug("[ACA] solver r3.7 init on", map);

      function getContainer(){ return map.getContainer(); }
      function rectBase(){
        const crect = getContainer().getBoundingClientRect();
        return function rect(el){
          const r = el.getBoundingClientRect();
          return { x: r.left - crect.left, y: r.top - crect.top, w: r.width, h: r.height };
        };
      }
      function center(R){ return { x: R.x + R.w/2, y: R.y + R.h/2 }; }
      function overlaps(A,B,p){
        return !(A.x > B.x + B.w + p || B.x > A.x + A.w + p || A.y > B.y + B.h + p || B.y > A.y + A.h + p);
      }
      function mtv(A,B){
        const ac = center(A), bc = center(B);
        const dx = bc.x - ac.x, dy = bc.y - ac.y;
        const px = (A.w/2 + B.w/2) - Math.abs(dx);
        const py = (A.h/2 + B.h/2) - Math.abs(dy);
        if (px <= 0 || py <= 0) return null;
        if (px < py) return { x: Math.sign(dx) * px, y: 0 };
        return { x: 0, y: Math.sign(dy) * py };
      }
      function rectCirclePenetration(R, Cx, Cy, Cr){
        const rx = Math.max(R.x, Math.min(Cx, R.x + R.w));
        const ry = Math.max(R.y, Math.min(Cy, R.y + R.h));
        const qx = Cx - rx, qy = Cy - ry;
        const d2 = qx*qx + qy*qy;
        const r  = Cr + PAD;
        if (d2 >= r*r) return null;
        const d = Math.max(1e-6, Math.sqrt(d2));
        const ux = qx / d, uy = qy / d;
        const pen = r - d;
        return { x: -ux * pen, y: -uy * pen };
      }
      function clamp(v, lo, hi){ return Math.max(lo, Math.min(hi, v)); }
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

      function collect(rect){
        const items=[];
        map.eachLayer(lyr=>{
          if (!(lyr instanceof L.CircleMarker)) return;
          const tt = (lyr.getTooltip && lyr.getTooltip()) || null;
          if (!tt) return;
          if (!tt._container) tt.update();
          const el = tt._container;
          if (!el || !el.classList.contains('iata-tt')) return;

          const cls = Array.from(el.classList);
          const size = (cls.find(c=>c.startsWith('size-'))||'size-small').slice(5);
          const txt = ensureWrap(el);
          txt.style.transform = 'translate(0px,0px)'; // reset
          const baseRect = rect(txt);
          const latlng = lyr.getLatLng();
          const radius = (typeof lyr.getRadius==='function') ? lyr.getRadius() : 6;
          const pt = map.latLngToContainerPoint(latlng);
          const color = (lyr.options && (lyr.options.fillColor || lyr.options.color)) || "#666";
          items.push({ el, txt, size, baseRect, latlng, radius, pt, color, dx:0, dy:0, density:0, marker:lyr });
        });
        return items;
      }

      function scoreDensity(items){
        const R = 70;
        for (let i=0;i<items.length;i++){
          let c=0;
          for (let j=0;j<items.length;j++){
            if (i===j) continue;
            const dx = items[i].pt.x - items[j].pt.x;
            const dy = items[i].pt.y - items[j].pt.y;
            if (dx*dx + dy*dy <= R*R) c++;
          }
          items[i].density = c;
        }
      }

      function rectFrom(it, dx, dy){
        return { x: it.baseRect.x + dx, y: it.baseRect.y + dy, w: it.baseRect.w, h: it.baseRect.h };
      }

      // cluster by proximity; if two dots are within NEAR_MILES, treat as same-city
      function buildClusters(items, finals, z){
        const n = items.length;
        const parent = Array.from({length:n}, (_,i)=>i);
        function find(a){ return parent[a]===a ? a : (parent[a]=find(parent[a])); }
        function uni(a,b){ a=find(a); b=find(b); if(a!==b) parent[b]=a; }

        const nearMeters = NEAR_MILES * 1609.344;

        for(let i=0;i<n;i++){
          for(let j=i+1;j<n;j++){
            const dx = items[i].pt.x - items[j].pt.x;
            const dy = items[i].pt.y - items[j].pt.y;
            const screenClose = (dx*dx + dy*dy) <= (STACK_R*STACK_R);
            const geoClose = map.distance(items[i].latlng, items[j].latlng) <= nearMeters;

            // If labels still overlap, always union.
            // If "same city" (<= 50mi) and we're zoomed out enough (<= ALIGN_LEFT_Z),
            // union even if labels don't overlap so we get a tidy left-justified list.
            const stillOverlap = overlaps(finals[i], finals[j], PAD);
            if (stillOverlap || (geoClose && z <= ALIGN_LEFT_Z) || (screenClose && stillOverlap)){
              uni(i,j);
            }
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

      function drawStackForCluster(clusterIdxs, items, finals, z){
        const pane = map.getPanes().tooltipPane;
        if (!pane) return;

        // hide individual labels in the cluster
        clusterIdxs.forEach(i => { items[i].el.style.display = 'none'; });

        // cluster bbox in screen coords
        let minX=1e9,minY=1e9,maxX=-1e9,maxY=-1e9;
        clusterIdxs.forEach(i=>{
          const R = finals[i];
          minX = Math.min(minX, R.x);
          minY = Math.min(minY, R.y);
          maxX = Math.max(maxX, R.x + R.w);
          maxY = Math.max(maxY, R.y + R.h);
        });

        // sort rows vertically (stable)
        const rows = clusterIdxs.slice().sort((a,b)=> items[a].pt.y - items[b].pt.y);

        const div = document.createElement('div');
        div.className = 'iata-stack';
        rows.forEach(i=>{
          const r = document.createElement('div');
          r.className = 'row';
          r.style.marginTop = ROW_GAP + "px";
          r.style.marginBottom = ROW_GAP + "px";
          const dot = document.createElement('span');
          dot.className = 'dot';
          dot.style.background = items[i].color;
          const t = document.createElement('span');
          t.textContent = items[i].txt.textContent;
          r.appendChild(dot);
          r.appendChild(t);
          div.appendChild(r);
        });
        pane.appendChild(div);

        const mapW = map.getSize().x, mapH = map.getSize().y;
        const width  = div.getBoundingClientRect().width;
        const height = div.getBoundingClientRect().height;

        // placement: if zoomed out enough, anchor LEFT of the cluster bbox
        const forceLeft = (z <= ALIGN_LEFT_Z);
        let x, y;
        y = Math.round(minY);
        if (forceLeft){
          x = Math.round(minX - LIST_OFFSET - width);
        }else{
          x = Math.round(maxX + LIST_OFFSET);
          // if off right edge, place left
          if (x + width > mapW - 2) {
            x = Math.round(minX - LIST_OFFSET - width);
          }
        }

        // clamp inside map view
        y = Math.max(6, Math.min(y, mapH - height - 6));
        x = Math.max(2, Math.min(x, mapW - width - 2));

        div.style.left = x + "px";
        div.style.top  = y + "px";
      }

      function clearStacks(){
        const pane = map.getPanes().tooltipPane;
        if (!pane) return;
        pane.querySelectorAll('.iata-stack').forEach(n => n.remove());
      }

      function hideOrShowDots(items, z){
        items.forEach(it=>{
          if (!it.marker || !it.marker.setStyle) return;
          if (z < DOT_HIDE_Z){
            it.marker.setStyle({ opacity: 0, fillOpacity: 0 });
          }else{
            it.marker.setStyle({ opacity: 1, fillOpacity: 0.95 });
          }
        });
      }

      function solve(){
        const rect = rectBase();
        let items = collect(rect);
        if (!items.length) return;

        clearStacks();

        const z = map.getZoom();
        hideOrShowDots(items, z); // NEW: dot visibility by zoom

        const minZ = (typeof map.getMinZoom === 'function' && map.getMinZoom()) || 0;
        let maxZ = (typeof map.getMaxZoom === 'function' && map.getMaxZoom());
        if (maxZ == null) maxZ = 19;
        const span = Math.max(1, Math.round(EXTFRAC * (maxZ - minZ)));

        items.forEach(it=>{
          const baseGate = (SHOWZ[it.size] || 7);
          const extGate  = Math.max(minZ, baseGate - span);
          it.__baseGate = baseGate; it.__extGate = extGate;
          if (z < extGate) it.el.style.display = 'none';
          else it.el.style.display = 'block';
          it.txt.style.opacity = '1';
          it.dx = 0; it.dy = 0;
        });

        items = items.filter(it => it.el.style.display !== 'none');
        if (!items.length) return;

        scoreDensity(items);
        items.sort((a,b)=>{
          const d = b.density - a.density; if (d) return d;
          const pr = PRIOR[a.size] - PRIOR[b.size]; if (pr) return pr;
          return (a.pt.y - b.pt.y) || (a.pt.x - b.pt.x);
        });

        const W = map.getSize().x, H = map.getSize().y;
        const boundsMargin = 2;

        // relaxation pass
        for (let t=0; t<ITERS; t++){
          const rects = items.map(it => rectFrom(it, it.dx, it.dy));
          const fx = new Array(items.length).fill(0);
          const fy = new Array(items.length).fill(0);

          for (let i=0;i<items.length;i++){
            for (let j=i+1;j<items.length;j++){
              const v = mtv(rects[i], rects[j]);
              if (!v) continue;
              fx[i] -= v.x * 0.5; fy[i] -= v.y * 0.5;
              fx[j] += v.x * 0.5; fy[j] += v.y * 0.5;
            }
          }

          for (let i=0;i<items.length;i++){
            const it = items[i];
            const R  = rects[i];
            const pen = rectCirclePenetration(R, it.pt.x, it.pt.y, it.radius + 2);
            if (pen){ fx[i] += pen.x; fy[i] += pen.y; }
            fx[i] += -0.05 * it.dx;
            fy[i] += -0.05 * it.dy;

            if (R.x < boundsMargin) fx[i] += (boundsMargin - R.x);
            if (R.y < boundsMargin) fy[i] += (boundsMargin - R.y);
            if (R.x + R.w > W - boundsMargin) fx[i] -= (R.x + R.w - (W - boundsMargin));
            if (R.y + R.h > H - boundsMargin) fy[i] -= (R.y + R.h - (H - boundsMargin));
          }

          for (let i=0;i<items.length;i++){
            items[i].dx = clamp(items[i].dx + FSTEP*fx[i], -DRIFT, DRIFT);
            items[i].dy = clamp(items[i].dy + FSTEP*fy[i], -DRIFT, DRIFT);
          }
        }

        // apply transforms
        const finals = items.map(it => rectFrom(it, it.dx, it.dy));
        for (let i=0;i<items.length;i++){
          const it = items[i];
          const R  = finals[i];
          let collides = false;
          for (let j=0;j<items.length;j++){
            if (i===j) continue;
            if (overlaps(R, finals[j], PAD)) { collides = true; break; }
          }
          const inExt = (z < it.__baseGate) && (z >= it.__extGate);
          if (!inExt || !collides){
            it.el.style.display = 'block';
            it.txt.style.transform = "translate(" + Math.round(it.dx) + "px, " + Math.round(it.dy) + "px)";
            if (collides) it.txt.style.opacity = "0.9";
          } else {
            it.txt.style.transform = "translate(" + Math.round(it.dx) + "px, " + Math.round(it.dy) + "px)";
          }
        }

        // stacked list stage (includes “same city” clustering)
        const clusters = buildClusters(items, finals, z);
        if (clusters.length){
          clusters.forEach(g => drawStackForCluster(g, items, finals, z));
        }
      }

      // schedule
      let raf1=0, raf2=0;
      function schedule(){
        if (raf1) cancelAnimationFrame(raf1);
        if (raf2) cancelAnimationFrame(raf2);
        raf1 = requestAnimationFrame(function(){ raf2 = requestAnimationFrame(solve); });
      }
      map.whenReady(function(){ setTimeout(schedule, 400); });
      map.on('zoomend moveend overlayadd overlayremove layeradd layerremove', schedule);

      const pane = map.getPanes().tooltipPane;
      if (pane && 'MutationObserver' in window){
        const mo = new MutationObserver(schedule);
        mo.observe(pane, { childList:true, subtree:true, attributes:true, attributeFilter:['style','class'] });
      }
    }
  } catch (err) {
    console.error("[ACA] solver r3.7-stacks+city+hide crashed:", err);
  }
})();
"""

    # Substitute tokens and add JS to the script bucket
    js = (js
          .replace("__MAP__", m.get_name())
          .replace("__SHOWZ__", json.dumps(SHOW_AT))
          .replace("__PAD__", str(int(PAD_PX)))
          .replace("__DRIFT__", str(int(DRIFT_PX)))
          .replace("__EXTFRAC__", str(EXT_FRACTION))
          .replace("__ITERS__", str(int(ITERS)))
          .replace("__FSTEP__", str(FSTEP))
          .replace("__DOT_HIDE_Z__", str(int(DOT_HIDE_BELOW_Z)))
          .replace("__ALIGN_LEFT_Z__", str(int(ALIGN_LEFT_AT_Z)))
          .replace("__STACK_R__", str(int(STACK_R_PX)))
          .replace("__LIST_OFFSET__", str(int(LIST_OFFSET_PX)))
          .replace("__NEAR_MILES__", str(float(NEAR_MILES)))
          .replace("__ROW_GAP__", str(int(STACK_ROW_GAP_PX)))
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
        # Keep Pages live even if fetch/parsing fails.
        sys.exit(0)

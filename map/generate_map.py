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
SHOW_AT = dict(large=2, medium=3, small=4)
PAD_PX = 2

# Screen-space clustering radius (px) to consider labels in one stack
STACK_CLUSTER_RADIUS_PX = 30
EXT_FRACTION = 0.20

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
    SOLVER_VER = "stacker-r1.4"

    # --- CSS + footer badge ---
    badge_html = r"""
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
/* Stack UI */
.iata-stack{
  position:absolute; z-index:400; pointer-events:auto;
  background:rgba(255,255,255,.94); border:1px solid rgba(0,0,0,.18);
  border-radius:8px; padding:6px 8px;
  font:12px "Open Sans","Helvetica Neue",Arial,sans-serif; color:#485260;
  box-shadow:0 6px 18px rgba(0,0,0,.12);
}
.iata-stack .row{ padding:2px 4px; border-radius:6px; white-space:nowrap; cursor:pointer; user-select:none; }
.iata-stack .row:hover{ background:rgba(0,0,0,.06); }
</style>
<div class="last-updated">Last updated: __UPDATED__ • __VER__</div>
""".replace("__UPDATED__", updated).replace("__VER__", SOLVER_VER)
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

    # --- JS: cluster-and-stack labeler (hardened boot) ---
    js = r"""
// ===== ACA stacker bootstrap (r1.4) =====
(function(){
  if (window.__ACA_STACKER__) return;        // idempotent
  window.__ACA_STACKER__ = true;

  const SHOWZ     = __SHOWZ__;
  const PAD       = __PAD__;
  const EXTFRAC   = __EXTFRAC__;
  const CLUSTER_R = __CLUSTERR__;   // px

  // resolve Leaflet map: direct var, or scan window for map_*
  function resolveMap(){
    try { return __MAP__; } catch(e) {}
    for (const k in window){
      if (!Object.prototype.hasOwnProperty.call(window,k)) continue;
      const v = window[k];
      if (/^map_/.test(k) && v && v.getContainer && v.getPanes) return v;
    }
    return null;
  }

  function until(cond, cb, tries=240, delay=100){
    (function tick(n){
      if (cond()) return cb();
      if (n<=0) return;
      setTimeout(()=>tick(n-1), delay);
    })(tries);
  }

  function init(){
    const map = resolveMap();
    if (!map) return; // keep waiting

    // once-only dashed outline so you can *see* the script is active
    const styleTag = document.createElement("style");
    styleTag.textContent = ".iata-tt{ outline:1px dashed rgba(0,0,0,.25) }";
    document.head.appendChild(styleTag);
    setTimeout(()=> styleTag.remove(), 1500);

    const pane = map.getPanes && map.getPanes().tooltipPane;
    if (!pane) return;

    const mapEl = map.getContainer();

    function rectBase(){
      const crect = mapEl.getBoundingClientRect();
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
    function dist2(a,b){ const dx=a.x-b.x, dy=a.y-b.y; return dx*dx+dy*dy; }
    function overlaps(A,B,p){
      return !(A.x > B.x + B.w + p || B.x > A.x + A.w + p || A.y > B.y + B.h + p || B.y > A.y + A.h + p);
    }
    function bboxFromPoints(pts){
      let minx=Infinity, miny=Infinity, maxx=-Infinity, maxy=-Infinity;
      for (const p of pts){ if (p.x<minx) minx=p.x; if (p.y<miny) miny=p.y; if (p.x>maxx) maxx=p.x; if (p.y>maxy) maxy=p.y; }
      return {x:minx, y=miny, w:(maxx-minx), h:(maxy-miny)};
    }

    // collect all layers that have tooltips and latlngs (robust across Folium versions)
    function collect(rect){
      const items=[];
      map.eachLayer(lyr=>{
        if (!lyr || typeof lyr.getTooltip!=='function' || typeof lyr.getLatLng!=='function') return;
        const tt = lyr.getTooltip();
        if (!tt) return;
        if (!tt._container) tt.update();
        const el = tt._container;
        if (!el || !el.classList || !el.classList.contains('iata-tt')) return;

        const txt = ensureWrap(el);
        txt.style.transform = 'translate(0px,0px)'; // reset
        const baseRect = rect(txt);
        const latlng = lyr.getLatLng();
        const pt = map.latLngToContainerPoint(latlng);

        // size class (for SHOWZ gate)
        const cls = Array.from(el.classList || []);
        const size = (cls.find(c=>c.startsWith('size-'))||'size-small').slice(5);

        items.push({ lyr, el, txt, baseRect, latlng, pt, size, visible:true });
      });
      return items;
    }

    class UF{
      constructor(n){ this.p=Array.from({length:n},(_,i)=>i); this.r=new Array(n).fill(0); }
      find(x){ return this.p[x]===x?x:(this.p[x]=this.find(this.p[x])); }
      union(a,b){ a=this.find(a); b=this.find(b);
        if(a===b) return; if(this.r[a]<this.r[b]) [a,b]=[b,a]; this.p[b]=a; if(this.r[a]===this.r[b]) this.r[a]++; }
      groups(n){ const g=new Map(); for(let i=0;i<n;i++){ const r=this.find(i); (g.get(r)||g.set(r,[])).push(i); } return Array.from(g.values()); }
    }

    function clearStacks(){
      pane.querySelectorAll('.iata-stack').forEach(n=>n.remove());
    }

    function renderStack(clusterIdxs, items){
      const pts = clusterIdxs.map(i => items[i].pt);
      const bb = bboxFromPoints(pts);

      const mapSize = map.getSize();
      let estW = 0; clusterIdxs.forEach(i=>{ estW = Math.max(estW, items[i].baseRect.w); });
      const rowH = 16; const estH = clusterIdxs.length * rowH;

      const GUTTER = 8;
      let left = bb.x + bb.w + GUTTER;
      let top  = Math.max(6, bb.y - 4);
      if (left + estW + 12 > mapSize.x) left = Math.max(6, bb.x - GUTTER - estW);
      if (top + estH + 6 > mapSize.y)   top  = Math.max(6, mapSize.y - estH - 6);

      const box = document.createElement('div');
      box.className = 'iata-stack';
      box.style.left = Math.round(left) + 'px';
      box.style.top  = Math.round(top) + 'px';

      const rows = clusterIdxs
        .slice()
        .sort((i,j)=> items[i].txt.textContent.localeCompare(items[j].txt.textContent));
      rows.forEach(i=>{
        const row = document.createElement('div');
        row.className = 'row';
        row.textContent = items[i].txt.textContent;
        row.title = 'Open details';
        row.addEventListener('click', ()=>{ items[i].lyr.openPopup(); });
        row.addEventListener('mouseenter', ()=>{ items[i].txt.style.opacity = '0.9'; });
        row.addEventListener('mouseleave', ()=>{ items[i].txt.style.opacity = '1'; });
        box.appendChild(row);
      });

      pane.appendChild(box);
    }

    function gateByZoom(items){
      const z = map.getZoom();
      const minZ = (typeof map.getMinZoom === 'function' && map.getMinZoom()) || 0;
      let maxZ = (typeof map.getMaxZoom === 'function' && map.getMaxZoom());
      if (maxZ == null) maxZ = 19;
      const span = Math.max(1, Math.round(EXTFRAC * (maxZ - minZ)));
      items.forEach(it=>{
        const baseGate = (SHOWZ[it.size] || 7);
        const extGate  = Math.max(minZ, baseGate - span);
        it._gateBase = baseGate; it._gateExt = extGate;
        it.visible = (z >= extGate);
      });
    }

    function layout(){
      clearStacks();
      const rect = rectBase();

      let items = collect(rect);
      if (!items.length) return;

      // zoom gate then prepare DOM state
      gateByZoom(items);
      items.forEach(it=>{
        it.el.style.display = it.visible ? 'block' : 'none';
        it.txt.style.transform = 'translate(0px,0px)';
        it.txt.style.opacity = '1';
      });

      // only visible items participate
      const vis = items.map((it,idx)=>({it,idx})).filter(x=>x.it.visible);
      if (!vis.length) return;
      vis.forEach(x=>{ x.it.baseRect = rect(x.it.txt); });

      // cluster by (1) label overlap OR (2) dot proximity within CLUSTER_R
      const uf = new UF(vis.length);
      for (let a=0; a<vis.length; a++){
        for (let b=a+1; b<vis.length; b++){
          const A = vis[a].it.baseRect, B = vis[b].it.baseRect;
          const overlap = overlaps(A,B,PAD);
          const close   = dist2(vis[a].it.pt, vis[b].it.pt) <= (CLUSTER_R*CLUSTER_R);
          if (overlap || close) uf.union(a,b);
        }
      }
      const comps = uf.groups(vis.length);

      // render stacks (size>=2), keep singletons
      for (const comp of comps){
        if (comp.length >= 2){
          const idxs = comp.map(i => vis[i].idx);
          idxs.forEach(i => { items[i].el.style.display = 'none'; });
          renderStack(idxs, items);
        }else{
          const i = vis[comp[0]].idx;
          items[i].el.style.display = 'block';
          items[i].txt.style.transform = 'translate(0px,0px)';
        }
      }
    }

    // run layout after we *actually* have tooltip DOM elements
    function hasTooltips(){
      return !!(pane && pane.querySelector('.iata-tt'));
    }

    // schedule on many signals to outsmart static hosting
    let raf=0;
    function schedule(){ if (raf) cancelAnimationFrame(raf); raf = requestAnimationFrame(layout); }

    // First run once tooltips appear
    until(()=>hasTooltips(), ()=>{ schedule(); }, 200, 50);

    map.whenReady(()=> setTimeout(schedule, 250));
    map.on('zoomend moveend overlayadd overlayremove layeradd layerremove tileload resize', schedule);

    if (pane && 'MutationObserver' in window){
      const mo = new MutationObserver(()=>schedule());
      mo.observe(pane, { childList:true, subtree:true, attributes:true, attributeFilter:['style','class'] });
    }

    if (document.readyState === 'complete') setTimeout(schedule, 150);
    else window.addEventListener('load', ()=> setTimeout(schedule, 150), { once:true });
  }

  // Keep trying until the map is resolvable
  until(()=> !!resolveMap(), init, 240, 100);
})();
"""

    js = (
        js.replace("__MAP__", m.get_name())
          .replace("__SHOWZ__", json.dumps(SHOW_AT))
          .replace("__PAD__",  str(int(PAD_PX)))
          .replace("__EXTFRAC__", str(EXT_FRACTION))
          .replace("__CLUSTERR__", str(int(STACK_CLUSTER_RADIUS_PX)))
    )

    # Inject via Folium's script bucket (works on GH Pages)
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

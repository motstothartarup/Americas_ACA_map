# map/generate_map.py
# Builds docs/index.html with the ACA Americas map (labels only, L/R nudging, ~20% extended keep).
# Safe for GitHub Actions: if data fetch fails, writes a fallback page so Pages still serves something.

import io
import json
import os
import sys
from datetime import datetime, timezone
from string import Template

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
SHIFT_PX = 14
MAX_STEPS = 10
EXT_FRACTION = 0.20  # ~20% of zoom range

OUT_DIR = "docs"
OUT_FILE = os.path.join(OUT_DIR, "index.html")


# ---------- helpers ----------
def write_error_page(msg: str) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = f"""<!doctype html><meta charset="utf-8">
<title>ACA Americas map</title>
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate"/>
<meta http-equiv="Pragma" content="no-cache"/>
<meta http-equiv="Expires" content="0"/>
<style>body{{font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;padding:24px;color:#233;max-width:900px;margin:auto;background:#f6f8fb}}
.card{{background:#fff;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.08);padding:20px}}
h1{{margin:0 0 10px 0}}code{{background:#f5f7fb;padding:2px 6px;border-radius:6px}}</style>
<div class="card">
  <h1>ACA Americas map</h1>
  <p><strong>Status:</strong> temporarily unavailable.</p>
  <p><strong>Reason:</strong> {msg}</p>
  <p>Last attempt: {updated}. This page updates automatically once per day.</p>
</div>"""
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
    m.get_root().html.add_child(
        folium.Element(
            f"""
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate"/>
<meta http-equiv="Pragma" content="no-cache"/>
<meta http-equiv="Expires" content="0"/>
<style>
.leaflet-tooltip.iata-tt{{
  background: transparent; border: 0; box-shadow: none;
  color: #6e6e6e;
  font-family: "Open Sans","Helvetica Neue",Arial,sans-serif;
  font-weight: 1000; font-size: 12px; letter-spacing: 0.5px;
  text-transform: uppercase; white-space: nowrap;
}}
.leaflet-tooltip-top:before,
.leaflet-tooltip-bottom:before,
.leaflet-tooltip-left:before,
.leaflet-tooltip-right:before{{ display:none !important; }}
.leaflet-tooltip.iata-tt .ttxt{{ display:inline-block; transform:translateX(0px); will-change:transform; }}
.leaflet-control-layers-expanded{{ box-shadow:0 4px 14px rgba(0,0,0,.12); border-radius:10px; }}
.last-updated {{
  position:absolute; right:12px; bottom:12px; z-index:9999;
  background:#fff; padding:6px 8px; border-radius:8px;
  box-shadow:0 2px 8px rgba(0,0,0,.12);
  font:12px "Open Sans","Helvetica Neue",Arial,sans-serif; color:#485260;
}}
</style>
<div class="last-updated">Last updated: {updated}</div>
"""
        )
    )

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
                f"<b>{r.airport}</b><br>IATA: {r.iata}<br>ACA: <b>{r.aca_level}</b><br>Country: {r.country}",
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
                class_name=f"iata-tt size-{size} tt-{r.iata}",
            )
        )
        dot.add_to(groups[r.aca_level])

    folium.LayerControl(collapsed=False).add_to(m)

    # --- JS (string.Template; no .replace chaining) ---
    js_template = Template(
        r"""
<script>
(function(){
  const map = $MAP;
  const SHOWZ = $SHOWZ;
  const PAD   = $PAD;
  const PRIOR = {large:0, medium:1, small:2};
  const STEP  = $STEP;
  const STEPS = $STEPS;
  const EXTFRAC = $EXTFRAC;

  function getContainer(){ return map.getContainer(); }
  function getTooltipPane(){ return map.getPanes().tooltipPane; }
  function rectBase(){
    const crect = getContainer().getBoundingClientRect();
    return function rect(el){
      const r = el.getBoundingClientRect();
      return { x: r.left - crect.left, y: r.top - crect.top, w: r.width, h: r.height };
    };
  }
  function overlaps(a,b,p){
    return !(a.x > b.x + b.w + p || b.x > a.x + a.w + p || a.y > b.y + b.h + p || b.y > a.y + a.h + p);
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
  function collect(){
    const pane = getTooltipPane(); if (!pane) return [];
    const nodes = pane.querySelectorAll('.leaflet-tooltip.iata-tt');
    return Array.from(nodes).map(el => {
      const cls = Array.from(el.classList);
      const size = (cls.find(c => c.startsWith('size-')) || 'size-small').slice(5);
      const txt = ensureWrap(el);
      return { el, txt, size };
    });
  }
  function tryPlace(rectForDx, kept, pad){
    const candidates = [0];
    for (let k=1;k<=STEPS;k++){ candidates.push(+k*STEP, -k*STEP); }
    for (const dx of candidates){
      const R = rectForDx(dx);
      let collide = false;
      for (const K of kept){ if (overlaps(R, K, pad)) { collide = true; break; } }
      if (!collide) return {ok:true, dx, R};
    }
    return {ok:false};
  }
  function solveLabels(){
    const items = collect(); if (!items.length) return;

    const z = map.getZoom();
    const minZ = (typeof map.getMinZoom === 'function' && map.getMinZoom()) || 0;
    let maxZ = (typeof map.getMaxZoom === 'function' && map.getMaxZoom());
    if (maxZ == null) maxZ = 19;
    const span = Math.max(1, Math.round(EXTFRAC * (maxZ - minZ)));

    items.forEach(it => {
      const baseGate = (SHOWZ[it.size] || 7);
      const extGate  = Math.max(minZ, baseGate - span);
      it.__baseGate = baseGate; it.__extGate = extGate;

      if (z < extGate){
        it.el.style.display = 'none';
      } else {
        it.el.style.display = 'block';
        it.txt.style.transform = 'translateX(0px)';
        it.txt.style.opacity = '1';
      }
    });

    const cand = items.filter(it => it.el.style.display !== 'none');
    if (!cand.length) return;

    const rect = rectBase();
    const rectWithDx = (txtEl) => (dx) => {
      const prev = txtEl.style.transform || '';
      txtEl.style.transform = `translateX(${dx}px)`;
      const R = rect(txtEl);
      txtEl.style.transform = prev;
      return R;
    };

    cand.sort((a,b)=>{
      const pr = PRIOR[a.size] - PRIOR[b.size];
      if (pr !== 0) return pr;
      const ra = rect(a.txt), rb = rect(b.txt);
      if (ra.y !== rb.y) return ra.y - rb.y;
      return ra.x - rb.x;
    });

    const kept = [];
    for (const it of cand){
      const placed = tryPlace(rectWithDx(it.txt), kept, PAD);
      const inExtension = (z < it.__baseGate) && (z >= it.__extGate);
      if (placed.ok){
        it.txt.style.transform = `translateX(${placed.dx}px)`;
        kept.push(placed.R);
      } else {
        if (inExtension){
          it.el.style.display = 'none';
        } else {
          it.txt.style.transform = 'translateX(0px)';
          it.txt.style.opacity = '0.9';
        }
      }
    }
  }

  let raf1=0, raf2=0;
  function schedule(){
    if (raf1) cancelAnimationFrame(raf1);
    if (raf2) cancelAnimationFrame(raf2);
    raf1 = requestAnimationFrame(()=>{ raf2 = requestAnimationFrame(solveLabels); });
  }
  map.whenReady(()=> setTimeout(schedule, 500));
  map.on('zoomend moveend overlayadd overlayremove layeradd layerremove', schedule);

  const pane = map.getPanes().tooltipPane;
  if (pane && 'MutationObserver' in window){
    const mo = new MutationObserver(schedule);
    mo.observe(pane, { childList:true, subtree:true, attributes:true, attributeFilter:['style','class'] });
  }
})();
</script>
"""
    )

    js = js_template.substitute(
        MAP=m.get_name(),
        SHOWZ=json.dumps(SHOW_AT),
        PAD=int(PAD_PX),
        STEP=int(SHIFT_PX),
        STEPS=int(MAX_STEPS),
        EXTFRAC=EXT_FRACTION,
    )
    m.get_root().html.add_child(folium.Element(js))

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
        # Do not fail the workflow even if data fetch breaks; we still wrote a page.
        sys.exit(0)

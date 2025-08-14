# map/generate_table.py
# Build docs/aca_table.html: dropdown to pick region, then a table
# listing airport IATA codes grouped by ACA level (5 at top → 1 at bottom).

import io
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import requests
from bs4 import BeautifulSoup

# -------- config --------
OUT_DIR = "docs"
OUT_FILE = os.path.join(OUT_DIR, "aca_table.html")

# ACA level ordering (top to bottom). Adjust if ACA adds new levels.
LEVELS_DESC = ['Level 5', 'Level 4+', 'Level 4', 'Level 3+', 'Level 3', 'Level 2', 'Level 1']

# -------- helpers --------
def fetch_aca_html(timeout: int = 45) -> str:
    url = "https://www.airportcarbonaccreditation.org/accredited-airports/"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ACA-Table-Bot/1.0)",
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
    aca = aca.dropna(subset=["iata", "aca_level", "region4"])
    return aca

def make_payload(df: pd.DataFrame) -> dict:
    # Regions present in the data (prefer "Americas" first)
    regions = sorted(df["region4"].unique(), key=lambda x: (x != "Americas", x))
    # Group IATA codes by region and ACA level
    by_region = {}
    for reg in regions:
        sub = df[df["region4"] == reg]
        level_map = {lvl: [] for lvl in LEVELS_DESC}
        for lvl, block in sub.groupby("aca_level"):
            if lvl not in level_map:
                level_map[lvl] = []
            codes = sorted(str(x).strip().upper() for x in block["iata"].dropna().unique())
            level_map[lvl].extend(codes)
        by_region[reg] = level_map
    return {
        "levels_desc": LEVELS_DESC,
        "regions": regions,
        "by_region": by_region,
    }

def build_html(payload: dict) -> str:
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    data_json = json.dumps(payload, separators=(",", ":"))
    html = """<!doctype html>
<meta charset="utf-8">
<title>ACA Airports — Region Table</title>
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate"/>
<meta http-equiv="Pragma" content="no-cache"/>
<meta http-equiv="Expires" content="0"/>
<style>
  :root {
    --card-bg:#fff; --ink:#39424e; --muted:#6b7785; --accent:#0d6efd; --border:#e6e8ec;
  }
  body {
    margin:0; padding:24px; font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
    color:var(--ink); background:#f6f8fb;
  }
  .wrap { max-width:1100px; margin:0 auto; }
  .card {
    background:var(--card-bg); border-radius:12px; box-shadow:0 4px 20px rgba(0,0,0,.08);
    padding:20px 20px;
  }
  h1 { margin:0 0 12px 0; font-size:22px; }
  .row { display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
  .muted { color:var(--muted); font-size:13px; }
  select {
    font:14px/1.2 inherit; padding:6px 10px; border-radius:8px; border:1px solid var(--border); background:#fff;
  }
  table {
    width:100%; border-collapse:separate; border-spacing:0; margin-top:14px; font-size:14px;
  }
  thead th {
    text-align:left; font-weight:600; padding:10px 12px; border-bottom:1px solid var(--border);
    background:#fafbfc; position:sticky; top:0;
  }
  tbody td {
    padding:10px 12px; border-bottom:1px solid var(--border);
    vertical-align:top;
  }
  td.lvl { font-weight:700; width:110px; white-space:nowrap; }
  td.count { text-align:right; width:80px; color:var(--muted); }
  td.codes code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
    font-size:13px; background:#f5f7fb; padding:2px 6px; border-radius:6px; margin:2px 6px 2px 0; display:inline-block;
  }
  .footer { margin-top:10px; }
</style>

<div class="wrap">
  <div class="card">
    <div class="row">
      <h1>ACA Airports by Region</h1>
      <div class="muted">Last updated: __UPDATED__</div>
    </div>
    <div class="row" style="margin-top:8px;">
      <label for="regionSelect" class="muted">Region:</label>
      <select id="regionSelect" aria-label="Choose region"></select>
    </div>

    <table id="acaTable" aria-live="polite">
      <thead>
        <tr>
          <th>ACA Level</th>
          <th>Airport Codes</th>
          <th style="text-align:right">Count</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>

    <div class="footer muted">Codes are IATA; levels sorted 5 → 1.</div>
  </div>
</div>

<script id="aca-data" type="application/json">__DATA_JSON__</script>
<script>
(function(){
  const DATA = JSON.parse(document.getElementById('aca-data').textContent);
  const sel = document.getElementById('regionSelect');
  const tbody = document.querySelector('#acaTable tbody');

  function option(value, text) { const o = document.createElement('option'); o.value = value; o.textContent = text; return o; }

  function render(region) {
    const levels = DATA.levels_desc;
    const buckets = DATA.by_region[region] || {};
    tbody.innerHTML = '';
    let total = 0;

    levels.forEach(lvl => {
      const codes = (buckets[lvl] || []).slice().sort();
      total += codes.length;
      const tr = document.createElement('tr');

      const tdLvl = document.createElement('td'); tdLvl.className = 'lvl'; tdLvl.textContent = lvl;
      const tdCodes = document.createElement('td'); tdCodes.className = 'codes';
      const tdCount = document.createElement('td'); tdCount.className = 'count'; tdCount.textContent = String(codes.length);

      if (codes.length) {
        codes.forEach(c => {
          const chip = document.createElement('code'); chip.textContent = c;
          tdCodes.appendChild(chip);
        });
      } else {
        tdCodes.innerHTML = '<span class="muted">—</span>';
      }

      tr.appendChild(tdLvl); tr.appendChild(tdCodes); tr.appendChild(tdCount);
      tbody.appendChild(tr);
    });

    const trTotal = document.createElement('tr');
    trTotal.innerHTML = '<td class="lvl">Total</td><td></td><td class="count">'+ total +'</td>';
    tbody.appendChild(trTotal);
  }

  const regions = DATA.regions || [];
  regions.forEach(r => sel.appendChild(option(r, r)));
  const defaultRegion = regions.includes('Americas') ? 'Americas' : (regions[0] || '');
  if (defaultRegion) sel.value = defaultRegion;

  sel.addEventListener('change', () => render(sel.value));
  render(sel.value);
})();
</script>
"""
    return html.replace("__UPDATED__", updated).replace("__DATA_JSON__", data_json)

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        html = fetch_aca_html()
        df = parse_aca_table(html)
        payload = make_payload(df)
        page = build_html(payload)
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            f.write(page)
        print("Wrote", OUT_FILE)
    except Exception as e:
        print("ERROR building table:", e, file=sys.stderr)
        updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        fallback = """<!doctype html><meta charset="utf-8">
<title>ACA Airports — Region Table</title>
<style>body{font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;padding:24px;color:#233;max-width:900px;margin:auto;background:#f6f8fb}
.card{background:#fff;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.08);padding:20px}</style>
<div class="card">
  <h1>ACA Airports by Region</h1>
  <p><strong>Status:</strong> temporarily unavailable.</p>
  <p><strong>Reason:</strong> __ERR__</p>
  <p>Last attempt: __UPDATED__. This page updates when the generator runs.</p>
</div>""".replace("__ERR__", str(e)).replace("__UPDATED__", updated)
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            f.write(fallback)
        sys.exit(0)

if __name__ == "__main__":
    main()

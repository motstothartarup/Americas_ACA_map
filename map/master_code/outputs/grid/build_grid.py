# map/master_code/outputs/grid/build_grid.py
# Build the aligned 10-column competitor grid as HTML (no prompts).

import os, argparse
import pandas as pd
from common.aci import load_aci
from common.competitors import build_sets

CSS = """
<style>
.container{max-width:1100px;margin:18px auto;font-family:Inter,system-ui,Arial}
.header .meta{color:#6b7280}
.row{display:grid;grid-template-columns:190px 1fr;column-gap:16px;align-items:start;margin:12px 0}
.cat{font-weight:800}
.grid{display:grid;grid-template-columns:repeat(10,minmax(84px,1fr));gap:10px}
.chip{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:56px;
      padding:8px 10px;border:1px solid #9aa2af;border-radius:14px;background:#f6f8fa;color:#111827;text-align:center}
.chip .code{font-weight:800;line-height:1.05}
.chip .dev{font-size:11px;color:#6b7280;line-height:1.05;margin-top:2px}
.chip.empty{visibility:hidden}
.chip.origin{border-color:#E74C3C;box-shadow:0 0 0 2px rgba(231,76,60,.2) inset}
</style>
"""

def _dev(val, target, pct_metric: bool):
    if pd.isna(val) or pd.isna(target): return ""
    diff = float(val) - float(target)
    if pct_metric:
        if abs(target) < 1e-9: return f"{diff:+.1f}pp"
        return f"{(diff/target)*100:+.1f}%"
    if abs(target) < 1e-9: return ""
    return f"{(diff/target)*100:+.1f}%"

def _grid_html(df_rows, metric_col, target_val, pct_metric, origin_iata):
    chips=[]
    for _, r in df_rows.iterrows():
        code=str(r["iata"])
        dev=_dev(r[metric_col], target_val, pct_metric)
        dev_html=f"<span class='dev'>{dev}</span>" if dev else "<span class='dev'>&nbsp;</span>"
        cls="chip origin" if code==origin_iata else "chip"
        chips.append(f"<div class='{cls}'><span class='code'>{code}</span>{dev_html}</div>")
    while len(chips)<10:
        chips.append("<div class='chip empty'><span class='code'>&nbsp;</span><span class='dev'>&nbsp;</span></div>")
    return "".join(chips[:10])

def build_grid_html(xlsx_path: str | None, iata: str, w_size: float, w_growth: float, w_share: float | None = None, topn=10):
    if w_share is None: w_share = max(0.0, 100.0 - float(w_size) - float(w_growth))
    df = load_aci(xlsx_path)
    if df[df["iata"]==iata].empty:
        raise ValueError(f"IATA '{iata}' not in ACI dataset")

    target, sets, union = build_sets(df, iata, w_size, w_growth, w_share, topn)
    r1, r2, r3, r4 = sets["total"], sets["growth"], sets["share"], sets["composite"]
    growth_target = r2["_target_growth"].iloc[0] if "_target_growth" in r2.columns else target["yoy_growth_pct"]

    total  = _grid_html(r1, "total_passengers", target["total_passengers"], False, iata)
    growth = _grid_html(r2, "yoy_growth_pct",   growth_target, True,  iata)
    share  = _grid_html(r3, "share_of_region_pct", target["share_of_region_pct"], True, iata)
    comp   = _grid_html(r4, "total_passengers", target["total_passengers"], False, iata)

    header = f"""
    <div class="header">
      <h3 style="margin:0">{target['iata']} — {target['name']}</h3>
      <div class="meta">State: {target['state']} · FAA: {target['faa_region']} ·
      Pax: {int(target['total_passengers']):,} · Share: {target['share_of_region_pct']}%</div>
    </div>
    """
    html = f"""<!doctype html><meta charset="utf-8"><title>Competitor Grid</title>
{CSS}
<div class="container">
  {header}
  <div class="row"><div class="cat">Total Passengers</div><div class="grid">{total}</div></div>
  <div class="row"><div class="cat">Growth (YoY %)</div><div class="grid">{growth}</div></div>
  <div class="row"><div class="cat">Share of Region</div><div class="grid">{share}</div></div>
  <div class="row"><div class="cat">Composite (weights: {w_size:.0f}/{w_growth:.0f}/{w_share:.0f})</div><div class="grid">{comp}</div></div>
</div>"""
    return {
        "html": html,
        "sets": {k: v["iata"].tolist() for k,v in sets.items()},
        "union": sorted(list(union)),
        "target": dict(target),
        "weights": (float(w_size), float(w_growth), float(w_share)),
    }

# CLI entry for quick checks (optional)
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=None, help="ACI workbook path (omit to use repo default)")
    ap.add_argument("--iata", required=True)
    ap.add_argument("--wsize", type=float, required=True)
    ap.add_argument("--wgrowth", type=float, required=True)
    ap.add_argument("--out", default="grid.html")
    args = ap.parse_args()

    res = build_grid_html(args.xlsx, args.iata.upper(), args.wsize, args.wgrowth)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(res["html"])
    print("Wrote", args.out)


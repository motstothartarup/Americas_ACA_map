
# map/master_code/common/competitors.py
# Build Top-10 lists (size, growth, share-any-region, composite).

import numpy as np
import pandas as pd

def nearest_by_total(df, iata, topn=10):
    t = df.loc[df["iata"]==iata].iloc[0]
    cand = df[df["iata"]!=iata].copy()
    cand["abs_diff_pax"] = (cand["total_passengers"] - t["total_passengers"]).abs()
    return t, cand.sort_values("abs_diff_pax").head(topn)

def nearest_by_growth(df, iata, topn=10):
    t = df.loc[df["iata"]==iata].iloc[0]
    cand = df[df["iata"]!=iata].copy()
    cand["yoy_growth_pct"] = pd.to_numeric(cand["yoy_growth_pct"], errors="coerce")
    cand["yoy_growth_pct"] = cand["yoy_growth_pct"].fillna(cand["yoy_growth_pct"].median())
    tg = t["yoy_growth_pct"] if pd.notna(t["yoy_growth_pct"]) else cand["yoy_growth_pct"].median()
    cand["abs_diff_growth"] = (cand["yoy_growth_pct"] - tg).abs()
    cand["_target_growth"] = tg
    return t, cand.sort_values("abs_diff_growth").head(topn)

def nearest_by_share_any(df, iata, topn=10):
    t = df.loc[df["iata"]==iata].iloc[0]
    cand = df[df["iata"]!=iata].copy()
    cand["abs_diff_share"] = (cand["share_of_region_pct"] - t["share_of_region_pct"]).abs()
    return t, cand.sort_values("abs_diff_share").head(topn)

def composite_weighted(df, iata, w_size=33.3, w_growth=33.3, w_share=33.4, topn=10):
    t = df.loc[df["iata"]==iata].iloc[0]
    cand = df[df["iata"]!=iata].copy()
    s = max(1e-9, float(w_size)+float(w_growth)+float(w_share))
    w_size, w_growth, w_share = w_size/s, w_growth/s, w_share/s

    size_sim = 1 - ((np.log1p(cand["total_passengers"]) - np.log1p(t["total_passengers"])).abs()
                    / (np.log1p(cand["total_passengers"]).abs().max() + 1e-9))
    g  = pd.to_numeric(cand["yoy_growth_pct"], errors="coerce").fillna(cand["yoy_growth_pct"].median())
    gt = t["yoy_growth_pct"] if pd.notna(t["yoy_growth_pct"]) else g.median()
    growth_sim = 1 - ((g - gt).abs() / (g.abs().max() + 1e-9))
    diff = (cand["share_of_region_pct"] - t["share_of_region_pct"]).abs()
    share_sim = 1 - (diff / (diff.max() + 1e-9))

    cand["score"] = (w_size*size_sim + w_growth*growth_sim + w_share*share_sim)
    return t, cand.sort_values("score", ascending=False).head(topn)

def build_sets(df, iata, w_size, w_growth, w_share, topn=10):
    t, r1 = nearest_by_total(df, iata, topn)
    _, r2  = nearest_by_growth(df, iata, topn)
    _, r3  = nearest_by_share_any(df, iata, topn)
    _, r4  = composite_weighted(df, iata, w_size, w_growth, w_share, topn)

    sets = {"total": r1, "growth": r2, "share": r3, "composite": r4}
    codes = {iata} | set(r1["iata"]) | set(r2["iata"]) | set(r3["iata"]) | set(r4["iata"])
    return t, sets, codes

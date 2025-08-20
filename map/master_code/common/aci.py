# map/master_code/common/aci.py
# Load ACI Excel, normalize columns, add FAA region + share_of_region_pct.

import os, re
import numpy as np
import pandas as pd

FAA_REGIONS = {
    "Alaskan":{"AK"},
    "New England":{"ME","NH","VT","MA","RI","CT"},
    "Eastern":{"NY","NJ","PA","DE","MD","DC","VA","WV"},
    "Southern":{"KY","TN","NC","SC","GA","FL","PR","VI"},
    "Great Lakes":{"OH","MI","IN","IL","WI"},
    "Central":{"MN","IA","MO","ND","SD","NE","KS"},
    "Southwest":{"NM","TX","OK","AR","LA"},
    "Northwest Mountain":{"WA","OR","ID","MT","WY","UT","CO"},
    "Western-Pacific":{"CA","NV","AZ","HI","GU"},
}

def _norm(s): return re.sub(r"\s+"," ",str(s)).strip().lower()

def _pick(df, cands):
    for c in cands:
        if c in df.columns:
            return c

def _default_excel_path():
    """Default to the repo copy under data/input/."""
    here = os.path.dirname(os.path.dirname(__file__))  # .../map/master_code
    # Try the clean name first; fall back to the long original.
    p1 = os.path.join(here, "data", "input", "ACI_2024_NA_Traffic.xlsx")
    p2 = os.path.join(here, "data", "input", "ACI 2024 North America Traffic Report (1).xlsx")
    return p1 if os.path.exists(p1) else p2

def load_aci(xlsx_path: str | None = None) -> pd.DataFrame:
    """Return dataframe with columns:
       iata, name, state, faa_region, total_passengers, yoy_growth_pct, share_of_region_pct
    """
    path = xlsx_path or _default_excel_path()
    if not os.path.exists(path):
        raise FileNotFoundError(f"ACI workbook not found at: {path}")

    raw = pd.read_excel(path, header=2)
    df = raw.rename(columns={c:_norm(c) for c in raw.columns}).copy()

    c_country   = _pick(df, ["country"])
    c_citystate = _pick(df, ["city/state","citystate","city, state","city / state"])
    c_airport   = _pick(df, ["airport name","airport"])
    c_iata      = _pick(df, ["airport code","iata","code"])
    c_total     = _pick(df, ["total passengers","passengers total","total pax"])
    c_yoy       = _pick(df, ["% chg 2024-2023","% chg 2024 - 2023","% chg 2023-2022","yoy %","% change"])

    if c_country:
        df = df[df[c_country].astype(str).str.contains("United States", case=False, na=False)]

    def _state(s):
        if not isinstance(s, str): return None
        parts = re.split(r"\s+", s.strip())
        return parts[-1] if parts else None

    df["state"] = df[c_citystate].apply(_state) if c_citystate else None
    df["name"]  = df[c_airport].astype(str)
    df["iata"]  = df[c_iata].astype(str).str.upper()
    df["total_passengers"] = pd.to_numeric(df[c_total], errors="coerce")
    df["yoy_growth_pct"]   = pd.to_numeric(df[c_yoy], errors="coerce") if c_yoy else np.nan
    df = df.dropna(subset=["iata","state","total_passengers"]).reset_index(drop=True)

    def _faa(st):
        s = str(st).upper()
        for reg, states in FAA_REGIONS.items():
            if s in states: return reg
        return "Unknown"

    df["faa_region"] = df["state"].apply(_faa)
    region_totals = df.groupby("faa_region")["total_passengers"].sum().rename("region_total")
    df = df.merge(region_totals, on="faa_region", how="left")
    df["share_of_region_pct"] = (df["total_passengers"] / df["region_total"] * 100).round(3)
    return df


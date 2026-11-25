#!/usr/bin/env python3
"""Builds data/counties.json and data/counties-albers-10m.json for index.html.

Run from the repo root (needs internet):   python build_data.py
Optional: set CENSUS_API_KEY (free at api.census.gov/data/key_signup.html) if the Census API rejects keyless requests.
Standard library only.

counties.json = list of {f, n, y:{year:[dem,gop,total]}, sh:[white,black,hispanic,asian,other], cv, ty:[urban,suburban,rural]}
  results   : tonmcg/US_County_Level_Election_Results_08-24 (2008-2024)
  sh, cv    : ACS 2022 5-year (B03002 race shares of population, B29001 citizen voting-age population)
  ty        : 2020 Census urban/rural population (Table P2) split by county land-area density (same rule as index.html)
"""
import csv, io, json, math, os, re, urllib.request, urllib.parse

YEARS = [2008, 2012, 2016, 2020, 2024]
KEY = os.environ.get("CENSUS_API_KEY")
RAW = "https://raw.githubusercontent.com/tonmcg/US_County_Level_Election_Results_08-24/master/{}_US_County_Level_Presidential_Results.csv"
ATLAS = "https://cdn.jsdelivr.net/npm/us-atlas@3/counties-albers-10m.json"
METRO, CORE = (100, 500), (1000, 10000)  # density ramps (people / sq mi), keep in sync with TYC in index.html

def fetch(url):
    if KEY and "api.census.gov" in url:
        url += ("&" if "?" in url else "?") + "key=" + urllib.parse.quote(KEY)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "build_data.py"}), timeout=120) as r:
        return r.read()

def census(url):
    rows = json.loads(fetch(url))
    return rows[1:]

def num(x):
    return float(re.sub(r"[,\s]", "", str(x)))

def ramp(d, a, b):
    return max(0.0, min(1.0, (math.log10(max(d, 1e-9)) - math.log10(a)) / (math.log10(b) - math.log10(a))))

def type_shares(pop, urb, area):
    u = min(1.0, urb / pop)
    d = pop / area
    metro = u * ramp(d, *METRO)
    core = ramp(d, *CORE)
    return [metro * core, metro * (1 - core), 1 - metro]

def pick_columns(cols):
    fk = next((k for k in cols if re.search("fips", k, re.I)), None)
    dk = next((k for k in cols if re.fullmatch(r"(votes_?dem|dem_?votes|dem)", k, re.I)), None) or \
         next((k for k in cols if re.search("dem", k, re.I) and not re.search("per|pct", k, re.I)), None)
    gk = next((k for k in cols if re.fullmatch(r"(votes_?gop|gop_?votes|gop|votes_?rep|rep)", k, re.I)), None) or \
         next((k for k in cols if re.search("gop|rep", k, re.I) and not re.search("per|pct", k, re.I)), None)
    tk = next((k for k in cols if re.fullmatch(r"(total_?votes|total)", k, re.I)), None)
    nk = next((k for k in cols if re.fullmatch(r"(county_?name|county)", k, re.I)), None)
    return fk, dk, gk, tk, nk

def add_results(M, year, text):
    rd = csv.DictReader(io.StringIO(text))
    fk, dk, gk, tk, nk = pick_columns(rd.fieldnames)
    if not (fk and dk and gk):
        print("  ! columns not found for", year, rd.fieldnames); return
    for r in rd:
        raw = str(r[fk]).strip()
        if not re.fullmatch(r"\d{1,5}", raw): continue
        f = raw.zfill(5)
        try:
            d, g = num(r[dk]), num(r[gk])
            t = num(r[tk]) if tk and r[tk] else d + g
        except ValueError:
            continue
        if not t > 0: continue
        o = M.setdefault(f, {"f": f, "n": r[nk] if nk else f, "y": {}})
        o["y"][str(year)] = [int(d), int(g), int(t)]
        if year == 2024 and nk: o["n"] = r[nk]

def main():
    os.makedirs("data", exist_ok=True)
    M = {}
    for y in YEARS:
        print("results", y)
        add_results(M, y, fetch(RAW.format(y)).decode("utf-8-sig"))
    print("ACS race shares");  
    for r in census("https://api.census.gov/data/2022/acs/acs5?for=county:*&get=B03002_001E,B03002_003E,B03002_004E,B03002_012E,B03002_006E"):
        o = M.get(r[5] + r[6]); t = num(r[0])
        if o and t:
            s = [num(r[i]) / t for i in (1, 2, 3, 4)]
            o["sh"] = [round(x, 4) for x in s] + [round(max(0.0, 1 - sum(s)), 4)]
    print("ACS citizen voting-age population")
    for r in census("https://api.census.gov/data/2022/acs/acs5?for=county:*&get=B29001_001E"):
        o = M.get(r[1] + r[2])
        if o: o["cv"] = int(num(r[0]))
    print("Census urban/rural + land area")
    dhc = census("https://api.census.gov/data/2020/dec/dhc?get=P2_001N,P2_002N&for=county:*")
    area = {}
    for v in ("2020", "2021"):
        try:
            area = {r[1] + r[2]: num(r[0]) for r in census(f"https://api.census.gov/data/{v}/geoinfo?get=AREALAND_SQMI&for=county:*")}
            break
        except Exception as e:
            print("  geoinfo", v, "failed:", e)
    for r in dhc:
        f = r[2] + r[3]; o = M.get(f); pop, urb = num(r[0]), num(r[1])
        if o and pop > 0 and area.get(f, 0) > 0:
            o["ty"] = [round(x, 4) for x in type_shares(pop, urb, area[f])]
    out = [o for o in M.values() if o["y"]]
    with open("data/counties.json", "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    miss = lambda k: sum(1 for o in out if k not in o)
    print(f"wrote data/counties.json: {len(out)} counties (missing race {miss('sh')}, cvap {miss('cv')}, type {miss('ty')})")
    print("map geometry")
    with open("data/counties-albers-10m.json", "wb") as fh:
        fh.write(fetch(ATLAS))
    print("done. Commit index.html and the data/ folder.")

if __name__ == "__main__":
    main()

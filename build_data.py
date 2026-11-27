#!/usr/bin/env python3
"""Builds data/counties.json and data/counties-albers-10m.json for index.html.

Run from the repo root (needs internet):   python build_data.py
Optional: set CENSUS_API_KEY to use a different Census key (a default key is built in).
Standard library only.

counties.json = list of
  {f, n, y:{year:[dem,gop,total]},
   shv:{vintage:[white,black,hispanic,asian,other]},   race shares of population, per Census vintage
   tyv:{vintage:[urban,suburban,rural]},               community type, per Census vintage
   cvv:{vintage:adult population},                     voting-age pop (2000/2010/2020) or citizen VAP (2022)
   agv:{vintage:[18-29,30-44,45-64,65+]},              age mix of adults (ACS 5-year: 2012, 2016, 2020, 2022)
   edv:{vintage:[no degree,college degree]},           education of adults 25+ (ACS 5-year: 2012, 2016, 2020, 2022)
   sh, cv, ty}                                         legacy fields (2022 race, 2022 CVAP, 2020 type)

Census vintages (index.html picks the one that matches the election year, or the one you choose):
  2000  Census 2000 SF1
  2010  Census 2010 SF1
  2020  Census 2020 PL 94-171 (race, adults) + DHC (urban/rural)
  2022  ACS 2022 5-year (B03002 race shares, B29001 citizen voting-age population)
Variable codes are looked up in the Census variables.json files when the built-in guess fails, and every vintage
is sanity-checked (national Hispanic / White / urban shares) before it is kept, so a bad code skips that vintage
instead of writing wrong numbers.

  results : tonmcg/US_County_Level_Election_Results_08-24 (2008-2024)
  type    : Census urban/rural population split by county land-area density (same rule as index.html)
"""
import csv, io, json, math, os, re, urllib.request, urllib.parse

YEARS = [2008, 2012, 2016, 2020, 2024]
KEY = os.environ.get("CENSUS_API_KEY", "c1f724c3832633ff1237e167f6821e940339e767")
RAW = "https://raw.githubusercontent.com/tonmcg/US_County_Level_Election_Results_08-24/master/{}_US_County_Level_Presidential_Results.csv"
ATLAS = "https://cdn.jsdelivr.net/npm/us-atlas@3/counties-albers-10m.json"
METRO, CORE = (100, 500), (1000, 10000)  # density ramps (people / sq mi), keep in sync with TYC in index.html

# race = [total, hispanic, white non-Hisp, black non-Hisp, asian non-Hisp]; None = look the codes up in variables.json
VINTAGES = {
    2000: dict(path="2000/dec/sf1", race=None, vap=None, urb_path="2000/dec/sf1", urb=None),
    2010: dict(path="2010/dec/sf1", race=["P005001", "P005010", "P005003", "P005004", "P005006"], vap="P010001",
               urb_path="2010/dec/sf1", urb=("P002001", "P002002")),
    2020: dict(path="2020/dec/pl", race=["P2_001N", "P2_002N", "P2_005N", "P2_006N", "P2_008N"], vap="P3_001N",
               urb_path="2020/dec/dhc", urb=("P2_001N", "P2_002N")),
}


def fetch(url):
    if KEY and "api.census.gov" in url and "key=" not in url:
        url += ("&" if "?" in url else "?") + "key=" + urllib.parse.quote(KEY)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "build_data.py"}), timeout=120) as r:
        return r.read()


def num(x):
    return float(re.sub(r"[,\s]", "", str(x)))


def get_cols(path, names):
    """{fips: [float, ...]} for the given variables of a county-level Census dataset."""
    rows = json.loads(fetch(f"https://api.census.gov/data/{path}?get={','.join(names)}&for=county:*"))
    hdr = rows[0]
    ix = [hdr.index(n) for n in names]
    si, ci = hdr.index("state"), hdr.index("county")
    out = {}
    for r in rows[1:]:
        try:
            v = [num(r[i]) for i in ix]
        except (TypeError, ValueError):
            continue
        if any(x < 0 for x in v):
            continue
        out[r[si] + r[ci]] = v
    return out


# ---- variable lookup (used only when the built-in codes fail or for vintages without built-in codes)
_meta = {}


def meta(path):
    if path not in _meta:
        _meta[path] = json.loads(fetch(f"https://api.census.gov/data/{path}/variables.json"))["variables"]
    return _meta[path]


def parts(label):
    return [p.strip(" :") for p in label.split("!!") if p.strip(" :")]


def by_concept(path, concept_ok):
    groups = {}
    for name, v in meta(path).items():
        c = (v.get("concept") or "").upper().strip()
        if "(" in c or not concept_ok(c):
            continue
        groups.setdefault(c, {})[name] = parts(v.get("label", ""))
    return [groups[c] for c in sorted(groups, key=lambda c: min(groups[c]))]


def first(group, ok):
    for name in sorted(group):
        if ok(group[name]):
            return name
    return None


def resolve_race(path):
    for g in by_concept(path, lambda c: re.match(r"^HISPANIC OR LATINO.*BY RACE$", c) and "18 YEARS" not in c):
        nh = lambda p, last: p[-1] == last and "Not Hispanic or Latino" in p
        names = [first(g, lambda p: p == ["Total"]),
                 first(g, lambda p: p == ["Total", "Hispanic or Latino"]),
                 first(g, lambda p: nh(p, "White alone")),
                 first(g, lambda p: nh(p, "Black or African American alone")),
                 first(g, lambda p: nh(p, "Asian alone"))]
        if all(names):
            return names
    raise LookupError("race variables not found in " + path)


def resolve_urban(path):
    for g in by_concept(path, lambda c: c.startswith("URBAN AND RURAL")):
        t, u = first(g, lambda p: p == ["Total"]), first(g, lambda p: p == ["Total", "Urban"])
        if t and u:
            return (t, u)
    raise LookupError("urban/rural variables not found in " + path)


def resolve_vap(path):
    for g in by_concept(path, lambda c: c.endswith("18 YEARS AND OVER")):
        t = first(g, lambda p: p == ["Total"])
        if t:
            return t
    raise LookupError("18+ population variable not found in " + path)


# ---- sanity checks (national shares must look like the real thing)
def frac(data, i, j=0):
    tot = sum(v[j] for v in data.values())
    return sum(v[i] for v in data.values()) / tot if tot else 0


def race_ok(d):
    return (0.05 < frac(d, 1) < 0.30 and 0.40 < frac(d, 2) < 0.85 and 0.08 < frac(d, 3) < 0.20 and 0.01 < frac(d, 4) < 0.10)


def urban_ok(d):
    return 0.60 < frac(d, 1) < 0.95


def vap_ok(vap, pop):
    t = sum(pop[f][0] for f in vap if f in pop)
    return t > 0 and 0.65 < sum(v[0] for f, v in vap.items() if f in pop) / t < 0.85


def attempt(label, guesses, check):
    """Try each way of getting a dataset (built-in codes first, then a variables.json lookup)."""
    for g in guesses:
        try:
            data = g()
            if data and check(data):
                return data
            print(f"  ! {label}: sanity check failed, trying next option" if data else f"  ! {label}: no data")
        except Exception as e:
            print(f"  ! {label}: {e}")
    print(f"  ! {label}: skipped")
    return None


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


def race_shares(v):
    """v = [total, hispanic, white, black, asian] -> [white, black, hispanic, asian, other]"""
    t = v[0]
    s = [v[2] / t, v[3] / t, v[1] / t, v[4] / t]
    return [round(x, 4) for x in s] + [round(max(0.0, 1 - sum(s)), 4)]


def main():
    os.makedirs("data", exist_ok=True)
    M = {}
    for y in YEARS:
        print("results", y)
        add_results(M, y, fetch(RAW.format(y)).decode("utf-8-sig"))
    for o in M.values():
        o["shv"], o["tyv"], o["cvv"] = {}, {}, {}

    print("land area")
    area = {}
    for v in ("2020", "2021"):
        try:
            rows = json.loads(fetch(f"https://api.census.gov/data/{v}/geoinfo?get=AREALAND_SQMI&for=county:*"))
            area = {r[1] + r[2]: num(r[0]) for r in rows[1:]}
            break
        except Exception as e:
            print("  geoinfo", v, "failed:", e)

    for y, sp in VINTAGES.items():
        print(f"Census {y}")
        race = attempt(f"{y} race", ([lambda: get_cols(sp["path"], sp["race"])] if sp["race"] else []) +
                                    [lambda: get_cols(sp["path"], resolve_race(sp["path"]))], race_ok)
        pop = None
        if race:
            pop = {f: [v[0]] for f, v in race.items()}
            for f, v in race.items():
                o = M.get(f)
                if o and v[0] > 0:
                    o["shv"][str(y)] = race_shares(v)
        urb = attempt(f"{y} urban/rural", ([lambda: get_cols(sp["urb_path"], list(sp["urb"]))] if sp["urb"] else []) +
                                          [lambda: get_cols(sp["urb_path"], list(resolve_urban(sp["urb_path"])))], urban_ok)
        if urb:
            for f, v in urb.items():
                o = M.get(f)
                if o and v[0] > 0 and area.get(f, 0) > 0:
                    o["tyv"][str(y)] = [round(x, 4) for x in type_shares(v[0], v[1], area[f])]
        if pop:
            vap = attempt(f"{y} adult population", ([lambda: get_cols(sp["path"], [sp["vap"]])] if sp["vap"] else []) +
                                                   [lambda: get_cols(sp["path"], [resolve_vap(sp["path"])])],
                          lambda d: vap_ok(d, pop))
            for f, v in (vap or {}).items():
                if f in M: M[f]["cvv"][str(y)] = int(v[0])

    print("ACS 2022 race shares + citizen voting-age population")
    A = "https://api.census.gov/data/2022/acs/acs5?for=county:*&get="
    try:
        rows = json.loads(fetch(A + "B03002_001E,B03002_003E,B03002_004E,B03002_012E,B03002_006E"))
        for r in rows[1:]:
            o = M.get(r[5] + r[6]); t = num(r[0])
            if o and t > 0:
                s = [num(r[i]) / t for i in (1, 2, 3, 4)]
                o["shv"]["2022"] = [round(x, 4) for x in s] + [round(max(0.0, 1 - sum(s)), 4)]
        for r in json.loads(fetch(A + "B29001_001E"))[1:]:
            o = M.get(r[1] + r[2])
            if o and num(r[0]) >= 0: o["cvv"]["2022"] = int(num(r[0]))
    except Exception as e:
        print("  ! ACS 2022 failed:", e)

    print("ACS age and education (5-year, one vintage per election era)")
    for yr in (2012, 2016, 2020, 2022):
        AY = "https://api.census.gov/data/%d/acs/acs5?for=county:*&get=" % yr
        try:
            # B01001 sex by age: male 007-025, female 031-049 (18-19 ... 85+); B15003: 001 total (25+), 022-025 bachelor's and above
            names = ["B01001_%03dE" % k for k in range(7, 26)] + ["B01001_%03dE" % k for k in range(31, 50)]
            rows = json.loads(fetch(AY + ",".join(names)))
            h = rows[0]; ix = [h.index(n) for n in names]; si, ci = h.index("state"), h.index("county")
            bands = [(0, 5), (5, 8), (8, 13), (13, 19)]  # 18-29, 30-44, 45-64, 65+
            tmp, nat = {}, [0.0] * 4
            for r in rows[1:]:
                try: v = [num(r[k]) for k in ix]
                except (TypeError, ValueError): continue
                if min(v) < 0: continue
                bb = [sum(v[a:z]) + sum(v[19 + a:19 + z]) for a, z in bands]
                if sum(bb) > 0:
                    tmp[r[si] + r[ci]] = bb; nat = [x + y for x, y in zip(nat, bb)]
            tn = sum(nat) or 1
            if 0.15 < nat[0] / tn < 0.30 and 0.15 < nat[3] / tn < 0.30:
                for f, bb in tmp.items():
                    if f in M: M[f].setdefault("agv", {})[str(yr)] = [round(x / sum(bb), 4) for x in bb]
            else:
                print("  ! age %d: sanity check failed, skipped" % yr)
            en = ["B15003_001E", "B15003_022E", "B15003_023E", "B15003_024E", "B15003_025E"]
            rows = json.loads(fetch(AY + ",".join(en)))
            h = rows[0]; ix = [h.index(n) for n in en]; si, ci = h.index("state"), h.index("county")
            tmp, t, c = {}, 0.0, 0.0
            for r in rows[1:]:
                try: v = [num(r[k]) for k in ix]
                except (TypeError, ValueError): continue
                if min(v) < 0 or v[0] <= 0: continue
                tmp[r[si] + r[ci]] = sum(v[1:]) / v[0]; t += v[0]; c += sum(v[1:])
            if t and 0.20 < c / t < 0.50:
                for f, sh in tmp.items():
                    if f in M: M[f].setdefault("edv", {})[str(yr)] = [round(1 - sh, 4), round(sh, 4)]
            else:
                print("  ! education %d: sanity check failed, skipped" % yr)
        except Exception as e:
            print("  ! ACS %d age/education failed:" % yr, e)

    out = [o for o in M.values() if o["y"]]
    for o in out:  # legacy fields for older copies of index.html
        if "2022" in o["shv"]: o["sh"] = o["shv"]["2022"]
        if "2022" in o["cvv"]: o["cv"] = o["cvv"]["2022"]
        if "2020" in o["tyv"]: o["ty"] = o["tyv"]["2020"]
        for k in ("shv", "tyv", "cvv", "agv", "edv"):
            if not o.get(k): o.pop(k, None)
    with open("data/counties.json", "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"wrote data/counties.json: {len(out)} counties")
    for k, name in (("shv", "race"), ("tyv", "community type"), ("cvv", "adult population"), ("agv", "age"), ("edv", "education")):
        for v in ("2000", "2010", "2020", "2022"):
            n = sum(1 for o in out if v in o.get(k, {}))
            if n: print(f"  {name} {v}: {n} counties")
    print("map geometry")
    with open("data/counties-albers-10m.json", "wb") as fh:
        fh.write(fetch(ATLAS))
    print("done. Commit index.html and the data/ folder.")


if __name__ == "__main__":
    main()

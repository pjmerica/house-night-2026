"""Every race live (House, Senate, Governor): NYT's count vs our model, and the desk's v0 projection, every 60 s.

Writes live.json (now) and hist.json (each race's path through the night) to the orphan branch `live`; index.html
reads them. House races also get Kalshi's prices (kalshi_map.json, from kalshi_map.py; user 2026-11-03: "wire up as
many kalshi markets as you can ... highlighting what to buy"):
  ask              the cheapest way to buy a side: YES on its market, or NO on the other side's (1 - its YES bid)
  edge             our chance - ask - Kalshi's taker fee (0.07 x multiplier x P(1-P)), per $1 contract
  pay up to        the highest ask that still leaves a 3c edge after the fee
  FLIP             the desk's rule (live_project.is_flip): our chance 45%+, edge 5c+, ask 8-40c, 20%+ counted,
                   and (House only) the count read leading the pick's side by 1+ pt. A district's projection leans on
                   our pre-election model for longer than the desk's county-by-county one (no county detail, and most
                   states have no count history), so a pick against the count is the model arguing with a market that
                   sees the count: tagged "against", never a FLIP (first pass, 2026-11-03: NJ-02 D+4.6 at 70% counted,
                   projection R 92%, Kalshi R 5c).

Per race (the desk's projector v0, live_project.project, with our margin model as the start):
  count estimate   c = margin now + (1 - p) * g,   sd = hypot((1 - p) * g_sd, 0.4)
                   g, g_sd: how the margin moved from p% counted to the end in the state's 2020-24 statewide races
                   (house_bench.json "lean"; the all-state spread, g = 0, where a state has no history)
  start            our model's margin, sd 6.1 (the desk's MODEL_MARGIN_SD)
  projection       the two combined by precision; P(win) = Phi(mu / sd)
  over/under       c - our model's margin: where the count, read with the state's usual late shift, is running
                   against what we expected (+ = the Democrat/non-Republican side ahead of the model)
  shift            projection - our model's margin: the same, weighed by how sure the count read is
Margins are D (non-Republican) minus R, points of all votes. p is NYT's expected-vote share counted (eevp).

County by county (House, 2026-11-03 night; user: "do we not have county level info?"): NYT's state pages carry only
district totals, but each district has its own file (results-{state}-us-house-{n}.json) with every county (towns in
New England) and its expected vote. For each counting, uncalled House race that file is read and the desk's projector
v1 (county_project.project) runs on it: each county's lean = its 2024 presidential two-party share (house_bench.json
"l24"; a county split between districts uses the whole county's), re-centred so the district is tied, weighted by
NYT's expected vote; started from our model (sd 6.1). It replaces v0 where the counties line up (src "v1"); the count
read is then v1's count-only margin, which knows which counties are still out (OH-09: Merrin +18 with Lucas County,
over half the district's vote, at 0%). The card lists the biggest counties still out.

    py night.py --minutes 340 [--state DIR] [--once]
"""
import argparse
import json
import math
import os
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import county_project      # polling-agg's projector v1 (Election night simulator/scripts), copied unchanged

HERE = os.path.dirname(os.path.abspath(__file__))
NYT = "https://static01.nyt.com/elections-assets/pages/data/2026-11-03/results-{}.json"
UA = {"User-Agent": "house-night-2026 (github.com/pjmerica/house-night-2026)"}
PASS = 60
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
FLIP_MIN_P, FLIP_MIN_EDGE, FLIP_MAX_ASK, FLIP_MIN_PCT = 0.45, 0.05, 0.40, 20.0
# House only: no FLIP under 8c. The 2024 flips that paid were bought at 15-33c; a district price of a few cents with
# votes counted is usually the market seeing which counties are out (OH-09 2026-11-03: Merrin +18.6 at 47% counted,
# Kaptur 96c with Toledo's Lucas County still to come), which a district total can't show
FLIP_MIN_ASK = 0.08
BOARD_EDGE = 0.03
SLUG = {"AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
        "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
        "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
        "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
        "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
        "NH": "new-hampshire", "NJ": "new-jersey", "NM": "new-mexico", "NY": "new-york", "NC": "north-carolina",
        "ND": "north-dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
        "RI": "rhode-island", "SC": "south-carolina", "SD": "south-dakota", "TN": "tennessee", "TX": "texas",
        "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west-virginia",
        "WI": "wisconsin", "WY": "wyoming"}
OFFICE = {"U.S. House": "H", "U.S. Senate": "S", "Governor": "G"}


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def fetch(st):
    try:
        with urllib.request.urlopen(urllib.request.Request(NYT.format(SLUG[st]) + f"?t={int(time.time())}", headers=UA),
                                    timeout=40) as r:
            return st, json.loads(r.read()), None
    except Exception as e:
        return st, None, str(e)[:120]


def nyt_races(st, d, bench_keys):
    """-> {race key: NYT race} for the state's House / Senate / Governor general-election races (L: Louisiana's Nov 3
    round, shown without a projection)."""
    out = {}
    for r in d.get("races") or []:
        o = OFFICE.get(r.get("office"))
        if not o or r.get("race_type_id") not in ("G", "L", None) and "special" not in str(r.get("type", "")).lower():
            continue
        special = "special" in (str(r.get("type", "")) + str(r.get("seat_description", "")) + str(r.get("title", ""))).lower()
        if o == "H":
            n = r.get("seat_number")
            try:
                n = int(n)
            except (TypeError, ValueError):
                n = 1
            k = f"{st}-{max(n, 1):02d}"
        else:
            k = st + ("-S" if special else "")
            if (o, k) not in bench_keys and (o, st if special else st + "-S") in bench_keys:
                k = st if special else st + "-S"      # only one Senate race in the state: whichever we have
        out[(o, k)] = r
    return out


def tally(r, race):
    """-> dict(d, r, tot, pct, exp, call) for the race's two sides (matched by last name, else DEM / GOP)."""
    meta = r.get("candidate_metadata") or {}
    unit = next((u for u in r.get("reporting_units") or [] if u.get("level") in ("state", "district")),
                (r.get("reporting_units") or [None])[0])
    if not unit:
        return None

    def side(cid):
        m = meta.get(cid) or {}
        ln = (m.get("last_name") or "").lower()
        if race.get("dl") and ln and (ln in race["dl"].lower() or race["dl"].lower() in ln):
            return "D"
        if race.get("rl") and ln and (ln in race["rl"].lower() or race["rl"].lower() in ln):
            return "R"
        p = (m.get("party") or {}).get("nyt_id")
        return "D" if p == "DEM" and race.get("dp") == "DEM" else "R" if p == "GOP" else None
    dv = rv = tot = 0
    lead = (None, -1)
    for c in unit.get("candidates") or []:
        v = (c.get("votes") or {}).get("total") or 0
        tot += v
        s = side(c["nyt_id"])
        dv += v if s == "D" else 0
        rv += v if s == "R" else 0
        if v > lead[1]:
            lead = ((meta.get(c["nyt_id"]) or {}).get("last_name") or c["nyt_id"], v)
    won = ((r.get("outcome") or {}).get("won") or []) if r.get("race_call_status") == "called" else []
    call = None
    if won:
        call = side(won[0]) or ((meta.get(won[0]) or {}).get("last_name") or won[0])
    return dict(d=dv, r=rv, tot=tot, pct=float(unit.get("eevp") or 0), exp=unit.get("total_expected_vote") or 0,
                call=call, lead=lead[0] if tot else None, upd=r.get("updated_at"))


def project(race, t, lean, model_sd, floor):
    """The desk's v0 from our model's start. -> dict(mu, sd, p, c, csd) or None."""
    if race["kind"] != "ok" or race["m"] is None:
        return None
    p = min(t["pct"], 100.0) if t else 0.0
    margin = 100 * (t["d"] - t["r"]) / t["tot"] if t and t["tot"] else 0.0
    if not t or t["tot"] < 100 or p <= 0:
        c, csd = margin, 60.0
    elif p >= 99.5:
        c, csd = margin, floor
    else:
        g, gsd = lean[min(int(p), 99)]
        rem = 1 - p / 100
        c, csd = margin + rem * g, math.hypot(rem * gsd, floor)
    wc, wp = 1 / csd ** 2, 1 / model_sd ** 2
    mu, sd = (wc * c + wp * race["m"]) / (wc + wp), math.sqrt(1 / (wc + wp))
    return dict(mu=mu, sd=sd, p=phi(mu / sd), c=c, csd=csd)


def kalshi_quotes(tickers):
    """{ticker: dict(bid, ask, status)} for every ticker, 50 a request."""
    out = {}
    for i in range(0, len(tickers), 50):
        url = f"{KALSHI}/markets?tickers={','.join(tickers[i:i + 50])}&limit=100"
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            for m in json.loads(r.read()).get("markets") or []:
                def f(k):
                    return float(m[k]) if m.get(k) not in (None, "") else None
                out[m["ticker"]] = dict(bid=f("yes_bid_dollars"), ask=f("yes_ask_dollars"), status=m.get("status"))
        time.sleep(0.12)
    return out


def kfee(p, mult):
    return 0.07 * mult * p * (1 - p)


def pay_up_to(prob, mult):
    """The highest ask with prob - ask - fee >= BOARD_EDGE, or None."""
    if prob - 0.01 - kfee(0.01, mult) < BOARD_EDGE:
        return None
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if prob - mid - kfee(mid, mult) >= BOARD_EDGE:
            lo = mid
        else:
            hi = mid
    return round(lo, 3)


def kalshi_sides(events, q):
    """events: the district's Kalshi events (kalshi_map.json). -> {D/R: dict(bid, ask, how, mult)} with the cheapest
    buy of each side across them; a side whose markets aren't trading is left out."""
    out = {}
    for ev in events:
        for side, other in (("D", "R"), ("R", "D")):
            a, b = q.get(ev.get(side)) or {}, q.get(ev.get(other)) or {}
            opts = []
            if a.get("status") == "active" and a.get("ask") and 0 < a["ask"] < 1:
                opts.append((a["ask"], "YES " + ev[side]))
            if b.get("status") == "active" and b.get("bid") and 0 < b["bid"] < 1:
                opts.append((round(1 - b["bid"], 4), "NO " + ev[other]))
            if not opts:
                continue
            ask, how = min(opts)
            if side not in out or ask < out[side]["ask"]:
                out[side] = dict(bid=a.get("bid") if a.get("status") == "active" else None, ask=ask, how=how,
                                 mult=ev.get("mult", 1.0))
    return out


DIST = "https://static01.nyt.com/elections-assets/pages/data/2026-11-03/results-{}-us-house-{}.json"


def unit_key(u):
    k = (u.get("fips_state") or "") + (u.get("fips_county") or "")
    if u.get("level") == "township" and u.get("fips_suffix"):
        k += "-" + str(u["fips_suffix"])
    return k


def fetch_district(job):
    race, = job
    st, n = race["k"].split("-")
    try:
        url = DIST.format(SLUG[st], int(n)) + f"?t={int(time.time())}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
            return race["id"], json.loads(r.read())
    except Exception:
        return race["id"], None


def district_units(d, race):
    """-> {unit key: dict(d, r, total, expected, eevp, name)} from a district file (towns in New England, counties
    elsewhere), the two sides matched as tally() does."""
    r = (d.get("races") or [None])[0]
    if not r:
        return {}
    meta = r.get("candidate_metadata") or {}

    def side(cid):
        m = meta.get(cid) or {}
        ln = (m.get("last_name") or "").lower()
        if race.get("dl") and ln and (ln in race["dl"].lower() or race["dl"].lower() in ln):
            return "D"
        if race.get("rl") and ln and (ln in race["rl"].lower() or race["rl"].lower() in ln):
            return "R"
        p = (m.get("party") or {}).get("nyt_id")
        return "D" if p == "DEM" and race.get("dp") == "DEM" else "R" if p == "GOP" else None
    out = {}
    for u in r.get("reporting_units") or []:
        if u.get("level") not in ("county", "township"):
            continue
        dv = rv = tot = 0
        for c in u.get("candidates") or []:
            v = (c.get("votes") or {}).get("total") or 0
            tot += v
            s = side(c["nyt_id"])
            dv += v if s == "D" else 0
            rv += v if s == "R" else 0
        out[unit_key(u)] = dict(d=dv, r=rv, total=tot, expected=u.get("total_expected_vote") or 0,
                                eevp=u.get("eevp") or 0, name=u.get("name"))
    return out


def v1_district(units, l24, race, model_sd):
    """The desk's v1 on one district. -> dict(mu, sd, p, c, csd, out) or None when the units don't line up."""
    if not units:
        return None
    keys = [k for k in units if k in l24]
    if len(keys) < 0.9 * len(units):
        return None
    exp = {k: max(units[k]["expected"], units[k]["total"]) for k in keys}
    tot = sum(exp.values())
    if tot <= 0:
        return None
    w = {k: exp[k] / tot for k in keys}
    raw = {k: l24[k][0] for k in keys}
    s_tie = county_project.solve_shift(raw, w, 0.5)
    L = {k: raw[k] + s_tie for k in keys}
    res = county_project.project({k: units[k] for k in keys}, L, w, m0=(race["m"], model_sd))
    if not res or not res["n_counties"]:
        return None
    ts = res["two_share"]
    c = county_project.shift_margin(L, w, res["count_shift"], ts)
    csd = (county_project.shift_margin(L, w, res["count_shift"] + res["count_shift_se"], ts)
           - county_project.shift_margin(L, w, res["count_shift"] - res["count_shift_se"], ts)) / 2
    out = []
    for k in sorted(keys, key=lambda k: -(exp[k] - units[k]["total"])):
        left = exp[k] - units[k]["total"]
        if left < 0.03 * tot or len(out) >= 4:
            break
        u = units[k]
        two = u["d"] + u["r"]
        share = u["d"] / two if two >= 50 else float(county_project.expit(L[k] + res["shift"]))
        out.append(dict(n=u["name"], pct=round(u["eevp"] or (100 * u["total"] / exp[k] if exp[k] else 0)),
                        left=round(left), sh=round(100 * left / tot, 1),
                        m24=round(100 * (2 * float(county_project.expit(raw[k])) - 1), 1),
                        exp=round(100 * (2 * share - 1), 1)))
    return dict(mu=res["mu"], sd=res["sd"], p=res["p_win"], c=c, csd=abs(csd), out=out,
                left=round(res["remaining"]), n=res["n_counties"], units=len(keys))


def r_(x, n=2):
    return None if x is None else round(x, n)


def git_publish(state, msg):
    g = lambda *a, check=True: subprocess.run(["git", "-C", state, *a], check=check, capture_output=True, text=True)
    g("add", "-A")
    if g("rev-parse", "--verify", "HEAD", check=False).returncode == 0:
        g("commit", "--amend", "-q", "-m", msg)
    else:
        g("commit", "-q", "-m", msg)
    r = g("push", "-q", "--force", "origin", "HEAD:live", check=False)
    if r.returncode:
        print("push failed:", r.stderr.strip()[:300], flush=True)


def write(path, obj):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=340)
    ap.add_argument("--state", default=None)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    bench = json.load(open(os.path.join(HERE, "house_bench.json"), encoding="utf-8"))
    races = bench["races"]
    kmap_path = os.path.join(HERE, "kalshi_map.json")
    kmap = json.load(open(kmap_path, encoding="utf-8")) if os.path.exists(kmap_path) else {}
    tickers = sorted({ev[s] for evs in kmap.values() for ev in evs for s in ("D", "R") if ev.get(s)})
    keys = {(r["office"], r["k"]): r for r in races}
    out_dir = a.state or HERE
    hist = {}
    if os.path.exists(os.path.join(out_dir, "hist.json")):
        try:
            hist = json.load(open(os.path.join(out_dir, "hist.json"), encoding="utf-8"))
        except Exception:
            hist = {}
    cache, errors = {}, {}
    end = time.time() + 60 * a.minutes
    while True:
        t0 = time.time()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with ThreadPoolExecutor(12) as ex:
            for st, d, err in ex.map(fetch, sorted({r["st"] for r in races})):
                if d is not None:
                    cache[st] = nyt_races(st, d, keys)
                    errors.pop(st, None)
                elif err:
                    errors[st] = f"{now} {err}"
        try:
            quotes = kalshi_quotes(tickers) if tickers else {}
            errors.pop("kalshi", None)
        except Exception as e:  # noqa: BLE001
            errors["kalshi"] = f"{now} {str(e)[:120]}"
            quotes = {}
        # county by county: each counting, uncalled House race's own NYT file
        want = []
        for race in races:
            if race["office"] != "H" or race["kind"] != "ok" or race["m"] is None:
                continue
            nr = cache.get(race["st"], {}).get((race["office"], race["k"]))
            t = tally(nr, race) if nr else None
            if t and t["tot"] >= 100 and 0 < t["pct"] < 99.5 and not t["call"]:
                want.append((race,))
        dist = {}
        with ThreadPoolExecutor(16) as ex:
            for rid, d in ex.map(fetch_district, want):
                if d is not None:
                    dist[rid] = d
        rows = []
        n_v1 = 0
        for race in races:
            nr = cache.get(race["st"], {}).get((race["office"], race["k"]))
            t = tally(nr, race) if nr else None
            pr = project(race, t, bench["lean"][race["st"]], bench["model_sd"], bench["sd_floor"])
            v1 = None
            if pr and race["id"] in dist:
                try:
                    v1 = v1_district(district_units(dist[race["id"]], race), bench["l24"], race, bench["model_sd"])
                except Exception as e:  # noqa: BLE001
                    errors["v1 " + race["k"]] = f"{now} {type(e).__name__}: {e}"[:160]
            if v1:
                n_v1 += 1
                pr = dict(mu=v1["mu"], sd=v1["sd"], p=v1["p"], c=v1["c"], csd=v1["csd"])
            margin = 100 * (t["d"] - t["r"]) / t["tot"] if t and t["tot"] else None
            counting = bool(t and t["tot"] >= 100 and t["pct"] > 0)
            row = dict(id=race["id"], pct=r_(t["pct"], 1) if t else None, v=t["tot"] if t else 0,
                       dv=t["d"] if t else 0, rv=t["r"] if t else 0, m=r_(margin), call=t["call"] if t else None,
                       lead=t["lead"] if t else None, nyt=bool(nr))
            if pr:
                row.update(mu=r_(pr["mu"]), sd=r_(pr["sd"]), p=r_(pr["p"], 4),
                           c=r_(pr["c"]) if counting else None, csd=r_(pr["csd"]) if counting else None,
                           ou=r_(pr["c"] - race["m"]) if counting else None,
                           sh=r_(pr["mu"] - race["m"]) if counting else None, src="v1" if v1 else "v0")
                if v1:
                    row.update(out=v1["out"], left=v1["left"], nu=v1["units"], nc=v1["n"])
            ks = kalshi_sides(kmap.get(race["k"], []), quotes) if race["office"] == "H" and pr and not row["call"] else {}
            if ks:
                row["k"] = {s: dict(bid=v["bid"], ask=v["ask"], how=v["how"]) for s, v in ks.items()}
                best = None
                for s, v in ks.items():
                    prob = pr["p"] if s == "D" else 1 - pr["p"]
                    fee = kfee(v["ask"], v["mult"])
                    edge = prob - v["ask"] - fee
                    if best is None or edge > best["edge"]:
                        best = dict(side=s, ask=v["ask"], fee=r_(fee, 4), edge=r_(edge, 4), ours=r_(prob, 4),
                                    upto=pay_up_to(prob, v["mult"]), how=v["how"])
                c = pr["c"] if counting else None
                best["against"] = bool(c is not None and abs(c) >= 0.5 and (c > 0) != (best["side"] == "D"))
                best["flip"] = bool(best["ours"] >= FLIP_MIN_P and best["edge"] >= FLIP_MIN_EDGE
                                    and FLIP_MIN_ASK <= best["ask"] <= FLIP_MAX_ASK and (t["pct"] if t else 0) >= FLIP_MIN_PCT
                                    and c is not None and (c if best["side"] == "D" else -c) >= 1.0)
                row["best"] = best
            rows.append(row)
            if counting:
                h = hist.setdefault(race["id"], [])
                pt = [now, row["pct"], row["m"], row.get("mu"), row.get("p"),
                      ((row.get("k") or {}).get("D") or {}).get("ask")]
                if not h or h[-1][1:3] != pt[1:3] or h[-1][4] != pt[4] or h[-1][5:6] != pt[5:6]:
                    h.append(pt)
                    del h[:-200]
        data = dict(generated=now, model_sd=bench["model_sd"], races=rows, errors=errors,
                    states_ok=len(cache), states=len({r["st"] for r in races}))
        try:
            write(os.path.join(out_dir, "live.json"), data)
            write(os.path.join(out_dir, "hist.json"), hist)
            if a.state:
                git_publish(a.state, "live " + now)
        except Exception as e:
            print("publish:", e, flush=True)
        n = sum(1 for x in rows if x.get("c") is not None)
        nb = sum(1 for x in rows if (x.get("best") or {}).get("edge", -1) >= BOARD_EDGE)
        print(now, f"{len(cache)} states, {n} races counting, {len(dist)}/{len(want)} district files, {n_v1} county by county, "
              f"{len(quotes)} Kalshi quotes, {nb} on the board, "
              f"{len(errors)} errors, {time.time() - t0:.1f}s", flush=True)
        if a.once or time.time() > end:
            break
        time.sleep(max(5, PASS - (time.time() - t0)))


if __name__ == "__main__":
    main()

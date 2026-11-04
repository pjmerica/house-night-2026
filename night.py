"""Every race live (House, Senate, Governor): NYT's count vs our model, and the desk's v0 projection, every 60 s.

Writes live.json (now) and hist.json (each race's path through the night) to the orphan branch `live`; index.html
reads them. No markets here (user call 2026-11-03: a separate thing).

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

HERE = os.path.dirname(os.path.abspath(__file__))
NYT = "https://static01.nyt.com/elections-assets/pages/data/2026-11-03/results-{}.json"
UA = {"User-Agent": "house-night-2026 (github.com/pjmerica/house-night-2026)"}
PASS = 60
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
        rows = []
        for race in races:
            nr = cache.get(race["st"], {}).get((race["office"], race["k"]))
            t = tally(nr, race) if nr else None
            pr = project(race, t, bench["lean"][race["st"]], bench["model_sd"], bench["sd_floor"])
            margin = 100 * (t["d"] - t["r"]) / t["tot"] if t and t["tot"] else None
            counting = bool(t and t["tot"] >= 100 and t["pct"] > 0)
            row = dict(id=race["id"], pct=r_(t["pct"], 1) if t else None, v=t["tot"] if t else 0,
                       dv=t["d"] if t else 0, rv=t["r"] if t else 0, m=r_(margin), call=t["call"] if t else None,
                       lead=t["lead"] if t else None, nyt=bool(nr))
            if pr:
                row.update(mu=r_(pr["mu"]), sd=r_(pr["sd"]), p=r_(pr["p"], 4),
                           c=r_(pr["c"]) if counting else None, csd=r_(pr["csd"]) if counting else None,
                           ou=r_(pr["c"] - race["m"]) if counting else None,
                           sh=r_(pr["mu"] - race["m"]) if counting else None)
            rows.append(row)
            if counting:
                h = hist.setdefault(race["id"], [])
                pt = [now, row["pct"], row["m"], row.get("mu"), row.get("p")]
                if not h or h[-1][1:3] != pt[1:3] or h[-1][4] != pt[4]:
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
        print(now, f"{len(cache)} states, {n} races counting, {len(errors)} errors, {time.time() - t0:.1f}s", flush=True)
        if a.once or time.time() > end:
            break
        time.sleep(max(5, PASS - (time.time() - t0)))


if __name__ == "__main__":
    main()

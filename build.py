"""Election night 2026, every race (House, Senate, Governor): model start, late-count history and shapes.
Run once before the night (night.py and index.html read the outputs). No markets (user call: a separate thing).

  house_bench.json  races: model margin (forecast_data.js: the margin model, or the no-polls model where a race has
                    no polls) and the two sides; lean: per state, the desk's v0 late-count shift
                    (live_project.lean_at on state_timeseries.csv: how the margin moved from p% counted to the end in
                    2020-24 statewide races) at every 1% counted, or the all-state spread where a state has no history
  house_geo.js      district shapes (polling-agg docs/us_house_2026_geo.js: the real 2026 lines) as rings for deck.gl

    py build.py
"""
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PA = r"C:\Users\pjmer\Documents\Polling Agg\Polling agg and Prediction markets"
sys.path.insert(0, os.path.join(PA, "Election night simulator", "scripts"))
sys.path.insert(0, PA)
import live_project as lp  # noqa: E402



def js_obj(path):
    s = open(path, encoding="utf-8").read()
    return json.loads(s[s.index("=") + 1:].strip().rstrip(";"))


def last_name(n):
    parts = [p for p in re.sub(r"[.,]", " ", n).split() if p.lower() not in ("jr", "sr", "ii", "iii", "iv")]
    return parts[-1] if parts else n


def races():
    out = []
    for r in js_obj(os.path.join(PA, "docs", "forecast_data.js"))["races"]:
        rid = r.get("agg_id") or ""
        if rid.startswith("2026-H-"):
            office, dist = "H", rid.split("-")[3]
            st = r["state"]
            key = f"{st}-{int(dist):02d}" if dist.isdigit() else f"{st}-01"
        elif rid.startswith("2026-SEN-") or rid.startswith("2026-GOV-"):
            office, st = ("S" if "SEN" in rid else "G"), r["state"]
            key = st + ("-S" if r.get("special") else "")
        else:
            continue
        cands = r.get("cands") or []
        reps = [c for c in cands if c["party"] == "REP"]
        others = sorted([c for c in cands if c["party"] != "REP"], key=lambda c: -(c.get("win") or 0))
        d = next((c for c in others if c["party"] == "DEM"), others[0] if others else None)
        if d and others and (others[0].get("win") or 0) > (d.get("win") or 0) + 0.25:
            d = others[0]                                # a DEM-slot independent the model favours over a DEM
        rep = max(reps, key=lambda c: c.get("win") or 0) if reps else None
        if key in OPPOSITION_FIX and d:                  # who the votes are counted for; the published margin and win chance stay
            d = dict(d, name=OPPOSITION_FIX[key], party="DEM")
        kind = "ok"
        if st == "LA" and office == "H":
            kind = "jungle"                              # Nov 3 is round one; the seat may go to a Dec 12 runoff
        elif not d or not rep:
            kind = "same-party" if len(cands) >= 2 else "uncontested"
        out.append(dict(id=rid, k=key, st=st, office=office, special=bool(r.get("special")), kind=kind, basis=r.get("basis"),
                        d=d["name"] if d else None, dp=d["party"] if d else None, dl=last_name(d["name"]) if d else None,
                        r=rep["name"] if rep else None, rl=last_name(rep["name"]) if rep else None,
                        m=d.get("margin") if d and rep else None,
                        win=d.get("win") if d and rep else None,
                        inc=r.get("incumbent_party")))
    return out


# The forecast named the wrong opposition. WI-06: Michael Thurow, an independent who drew 1% (a May poll merged two matchups, "Grothman
# 61, Smith 39, Thurow 45"); the Democratic nominee Brad Smith was the real opponent, so the count read Grothman by 51, not 9.
OPPOSITION_FIX = {"WI-06": "Brad Smith"}


def lean_tables():
    hist = lp.load_history()
    out = {}
    states = {r["st"] for r in races()}
    for st in sorted(states):
        rows = []
        for p in range(100):
            lean = lp.lean_at(hist, lp.ABBR[st], float(p)) if st in lp.ABBR else None   # history is keyed by state slug
            if lean is None:
                v = lp.pooled_var(hist, float(p), None)
                rows.append([0.0, round(lp.SD_SCALE * math.sqrt(v), 3)])
            else:
                rows.append([round(lean[0], 3), round(lean[1], 3)])
        out[st] = rows
    return out


def unit_key(u):
    """A reporting unit's key: state + county FIPS, plus the town code where NYT reports by town (New England), so
    2024's units and tonight's district files line up."""
    k = (u.get("fips_state") or "") + (u.get("fips_county") or "")
    if u.get("level") == "township" and u.get("fips_suffix"):
        k += "-" + str(u["fips_suffix"])
    return k


def leans_2024():
    """{unit key: [logit of Harris's two-party share in 2024, votes]} for every county (towns in New England), from the
    2024 presidential file of each state in polling-agg's NYT archive. The county projector's lean for each unit."""
    nyt = os.path.join(PA, "State election results", "data", "raw", "nyt_all", "2024")
    out = {}
    for f in sorted(os.listdir(nyt)):
        if not f.endswith("-president.json"):
            continue
        r = json.load(open(os.path.join(nyt, f), encoding="utf-8"))["races"][0]
        for u in r["reporting_units"]:
            if u.get("level") not in ("county", "township"):
                continue
            v = {c["nyt_id"]: (c.get("votes") or {}).get("total") or 0 for c in u["candidates"]}
            d, rr = v.get("harris-k", 0), v.get("trump-d", 0)
            if d + rr >= 20:
                p = min(max(d / (d + rr), 0.005), 0.995)
                out[unit_key(u)] = [round(math.log(p / (1 - p)), 4), d + rr]
    return out


def geo():
    g = js_obj(os.path.join(PA, "docs", "us_house_2026_geo.js"))
    u = g["unit"]

    def rings(d):
        out, cur, x, y = [], None, 0, 0
        for cmd, body in re.findall(r"([MlLz])([^MlLz]*)", d):
            nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", body)]
            if cmd == "M":
                x, y = nums[0], nums[1]
                cur = [[round(x * u, 1), round(-y * u, 1)]]
                for i in range(2, len(nums) - 1, 2):    # implicit lineto after M
                    x, y = nums[i], nums[i + 1]
                    cur.append([round(x * u, 1), round(-y * u, 1)])
            elif cmd == "l":
                for i in range(0, len(nums) - 1, 2):
                    x, y = x + nums[i], y + nums[i + 1]
                    cur.append([round(x * u, 1), round(-y * u, 1)])
            elif cmd == "L":
                for i in range(0, len(nums) - 1, 2):
                    x, y = nums[i], nums[i + 1]
                    cur.append([round(x * u, 1), round(-y * u, 1)])
            elif cmd == "z" and cur:
                if len(cur) > 2:
                    out.append(cur)
                cur = None
        if cur and len(cur) > 2:
            out.append(cur)
        return out

    dist = {k: dict(rings=rings(v["d"]), c=[v["lx"], -v["ly"]]) for k, v in g["districts"].items()}
    states = {k: rings(v) for k, v in g["states"].items()}
    return dict(districts=dist, states=states)


def main():
    rs = races()
    from collections import Counter
    print(Counter((r["office"], r["kind"]) for r in rs))
    l24 = leans_2024()
    print(len(l24), "county / town leans from 2024")
    bench = dict(races=rs, lean=lean_tables(), model_sd=lp.MODEL_MARGIN_SD, sd_floor=lp.SD_FLOOR, l24=l24)
    with open(os.path.join(HERE, "house_bench.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(bench, f, separators=(",", ":"))
    with open(os.path.join(HERE, "house_bench.js"), "w", encoding="utf-8", newline="\n") as f:
        f.write("window.HOUSE_BENCH = " + json.dumps(bench, separators=(",", ":")) + ";\n")
    gg = geo()
    with open(os.path.join(HERE, "house_geo.js"), "w", encoding="utf-8", newline="\n") as f:
        f.write("window.HOUSE_GEO = " + json.dumps(gg, separators=(",", ":")) + ";\n")
    print("geo:", len(gg["districts"]), "districts,", len(gg["states"]), "states")


if __name__ == "__main__":
    main()

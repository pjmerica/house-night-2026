"""The House through election night, with every recommendation: review_data.js for review.html.

Every 10 minutes from the first poll close, for all 435 races, from what was recorded at that moment:
  count, calls   NYT's state pages (release election-night-2026, states/): each district's total and call
  prices         Kalshi's minute candles (release kalshi-minutes-*): bid and ask, both sides
  projection     two versions, kept apart:
    "all night"  night.py's own v0 (`project`) run on that moment's district total, for the whole night. The FLIP rule's
                 count guard is exact here. Nothing from later in the night goes in. Recomputed, not what the page showed:
                 the page started at 10:28 pm ET and had no Kalshi until 11:07 pm.
    "as shown"   the chance the page actually published (hist.json): v0 until 12:08 am ET, then county by county (v1). None
                 while the job was down, 5:48 am - 2:14 pm ET on Nov 4. hist.json has no count read, so the FLIP guard
                 uses the margin so far (leading by 1+), and the rule's own guards changed during the night.
A recommendation (night.py's rules, today's version): board = our chance - ask - fee >= 3c, uncalled race; FLIP = also our
chance 45%+, edge 5c+, ask 8-40c, 20%+ counted, the count leading the pick by 1+. Each is bought once, one contract, at the
ask when first shown, and valued each tick at its bid, then 1 or 0 from NYT's call. Later NYT calls and Kalshi settlements
only decide how open positions finish.

County-by-county can't be recomputed for the hours before recording: the only county path for those hours is an estimate
built from final results (house_county_path), which would leak the answer.

    py build_review.py --states DIR --candles DIR     DIR: holds states/2026-11-03/... ; holds kalshi_minutes/
"""
import argparse
import bisect
import calendar
import collections
import csv
import glob
import gzip
import json
import os
import re
import time

import night

HERE = os.path.dirname(os.path.abspath(__file__))
START = "2026-11-03T23:00:00Z"        # first polls close, 6 pm ET
STEP = 600
FRESH = 900                           # a quote older than this isn't a price
EV = re.compile(r"^(?:KX)?HOUSE(?:RACE-)?([A-Z]{2})(\d{1,2}|AL)-")
CAP = 30                              # board rows kept per tick for the slider (FLIPs always)


def secs(ts):
    return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def stamp_secs(name):
    return calendar.timegm(time.strptime(name[:16], "%Y%m%dT%H%M%SZ"))


def f(x):
    try:
        return None if x in ("", None) else float(x)
    except ValueError:
        return None


def load_counts(states_dir, keys):
    """{(office, key): ([t], [tally])} for House races, from every recorded state page."""
    out = collections.defaultdict(lambda: ([], []))
    by_st = collections.defaultdict(list)
    for k, r in keys.items():
        if k[0] == "H":
            by_st[r["st"]].append(r)
    for st, rs in sorted(by_st.items()):
        folder = os.path.join(states_dir, "states", "2026-11-03", night.SLUG[st])
        for p in sorted(glob.glob(os.path.join(folder, "*.json.gz"))):
            try:
                with gzip.open(p) as g:
                    d = json.load(g)
            except (OSError, ValueError):
                continue
            races = night.nyt_races(st, d, keys)
            t = stamp_secs(os.path.basename(p))
            for r in rs:
                nr = races.get(("H", r["k"]))
                tl = night.tally(nr, r) if nr else None
                if tl:
                    a, b = out[r["id"]]
                    a.append(t)
                    b.append(tl)
        print(st, flush=True)
    return out


class Prices:
    def __init__(self, folder):
        self.folder = os.path.join(folder, "kalshi_minutes")
        self.c = {}
        idx = json.load(open(os.path.join(self.folder, "index.json"), encoding="utf-8"))["markets"]
        ev = collections.defaultdict(dict)
        self.result = {}
        for m in idx:
            t = m["ticker"]
            if t.endswith("-D") or t.endswith("-R"):
                ev[m["event"]][t[-1]] = t
                self.result[t] = m.get("result")
        self.races = collections.defaultdict(list)
        for e, s in ev.items():
            mm = EV.match(e)
            if mm:
                self.races[f"2026-H-{mm.group(1)}-{1 if mm.group(2) == 'AL' else int(mm.group(2)):02d}"].append(s)

    def rows(self, t):
        if t not in self.c:
            xs, rs = [], []
            p = os.path.join(self.folder, t + ".csv.gz")
            if os.path.exists(p):
                with gzip.open(p, "rt", encoding="utf-8") as g:
                    for x in csv.DictReader(g):
                        xs.append(int(x["end_ts"]))
                        rs.append((f(x["bid_close"]), f(x["ask_close"])))
            self.c[t] = (xs, rs)
        return self.c[t]

    def quote(self, t, ts):
        xs, rs = self.rows(t)
        i = bisect.bisect_right(xs, ts) - 1
        return rs[i] if i >= 0 and ts - xs[i] <= FRESH else None

    def book(self, rid, ts):
        """{D/R: dict(ask, bid)}: the cheapest buy of each side (YES on it or NO on the other), its best bid."""
        out = {}
        for s in self.races.get(rid, []):
            qd = self.quote(s["D"], ts) if s.get("D") else None
            qr = self.quote(s["R"], ts) if s.get("R") else None
            for side, q, o in (("D", qd, qr), ("R", qr, qd)):
                opts = [q[1]] if q and q[1] and 0 < q[1] < 1 else []
                if o and o[0] and 0 < o[0] < 1:
                    opts.append(round(1 - o[0], 4))
                bid = q[0] if q and q[0] else (round(1 - o[1], 4) if o and o[1] and 0 < o[1] < 1 else None)
                if opts and (side not in out or min(opts) < out[side]["ask"]):
                    out[side] = dict(ask=min(opts), bid=bid)
        return out

    def settled(self, rid, side):
        for s in self.races.get(rid, []):
            r = self.result.get(s.get(side))
            if r in ("yes", "no"):
                return r == "yes"
        return None


BANDS = (("under40", "Ask under 40¢", 0, .40), ("40to80", "Ask 40–80¢", .40, .80), ("over80", "Ask 80¢+", .80, 1.01))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", required=True)
    ap.add_argument("--candles", required=True)
    a = ap.parse_args()
    bench = json.load(open(os.path.join(HERE, "house_bench.json"), encoding="utf-8"))
    races = [r for r in bench["races"] if r["office"] == "H"]
    keys = {(r["office"], r["k"]): r for r in races}
    counts = load_counts(a.states, keys)
    prices = Prices(a.candles)
    hist = json.load(open(os.path.join(HERE, "hist.json"), encoding="utf-8"))
    live = {r["id"]: r for r in json.load(open(os.path.join(HERE, "live.json"), encoding="utf-8"))["races"]}
    last = max(ts[-1] for ts, _ in counts.values())
    ticks = list(range(secs(START), last + 1, STEP))
    idx = {r["id"]: i for i, r in enumerate(races)}

    def tally_at(rid, t):
        ts, vs = counts.get(rid, ([], []))
        i = bisect.bisect_right(ts, t) - 1
        return vs[i] if i >= 0 else None

    def side_of_call(tl):
        c = tl["call"] if tl else None
        return c if c in ("D", "R") else None

    hist_t = {rid: ([secs(x[0]) for x in pts], pts) for rid, pts in hist.items()}

    def shown_p(rid, t):
        s = hist_t.get(rid)
        if not s:
            return None
        i = bisect.bisect_right(s[0], t) - 1
        if i < 0 or t - s[0][i] > 1200:
            return None
        return s[1][i]

    pos = {"all": {}, "shown": {}}          # (rid, side, kind) -> position
    curves = {src: {k: [] for k in ("flip", "under40", "40to80", "over80", "board")} for src in pos}
    seats = dict(called_d=[], called_r=[], all=[], shown=[], kalshi=[])
    board_ticks = {"all": [], "shown": []}
    flips_live = {"all": [], "shown": []}
    last_val = {}

    for t in ticks:
        cd = cr = 0
        sum_all = sum_kal = sum_shown = 0.0
        tick_rows = {"all": [], "shown": []}
        for r in races:
            rid = r["id"]
            tl = tally_at(rid, t)
            call = side_of_call(tl)
            cd += call == "D"
            cr += call == "R"
            if r["kind"] != "ok":
                val = {"D": 1.0, "R": 0.0}.get(call)
                if val is None:
                    val = r["win"] if r.get("win") is not None else (1.0 if r.get("d") and not r.get("r") else 0.0)
                sum_all += val
                sum_kal += val
                sum_shown += val
                continue
            pr = night.project(r, tl, bench["lean"][r["st"]], bench["model_sd"], bench["sd_floor"])
            book = prices.book(rid, t) if not call else {}
            d_mid = None
            if book.get("D") and book["D"].get("bid") is not None:
                d_mid = (book["D"]["bid"] + book["D"]["ask"]) / 2
            elif book.get("R") and book["R"].get("bid") is not None:
                d_mid = 1 - (book["R"]["bid"] + book["R"]["ask"]) / 2
            if d_mid is not None:
                last_val[rid] = d_mid
            sh = shown_p(rid, t)
            if call:
                v = 1.0 if call == "D" else 0.0
                sum_all += v
                sum_kal += v
                sum_shown += v
            else:
                sum_all += pr["p"]
                sum_kal += last_val.get(rid, pr["p"])
                sum_shown += sh[4] if sh and sh[4] is not None else pr["p"]
            counting = bool(tl and tl["tot"] >= 100 and tl["pct"] > 0)
            margin = 100 * (tl["d"] - tl["r"]) / tl["tot"] if tl and tl["tot"] else None
            if call:
                continue
            for src, p, guard in (("all", pr["p"], pr["c"] if counting else None),
                                  ("shown", sh[4] if sh else None, sh[2] if sh else None)):
                if p is None:
                    continue
                pct = (tl["pct"] if tl else 0) or 0
                for side, q in book.items():
                    prob = p if side == "D" else 1 - p
                    ask = q["ask"]
                    fee = night.kfee(ask, 1.0)
                    e = prob - ask - fee
                    if e < night.BOARD_EDGE:
                        continue
                    lead = guard is not None and (guard if side == "D" else -guard) >= 1.0
                    flip = bool(prob >= night.FLIP_MIN_P and e >= night.FLIP_MIN_EDGE and night.FLIP_MIN_ASK <= ask <= night.FLIP_MAX_ASK
                                and pct >= night.FLIP_MIN_PCT and lead)
                    tick_rows[src].append([idx[rid], side, round(ask, 3), round(prob, 3), round(e, 3), int(flip), round(pct), None if margin is None else round(margin, 1)])
                    row = dict(rid=rid, k=r["k"], side=side, name=r["dl"] if side == "D" else r["rl"], t=t, ask=ask, fee=fee, prob=prob, edge=e, pct=pct, m=margin)
                    pos[src].setdefault((rid, side, "board"), dict(row))
                    if flip:
                        pos[src].setdefault((rid, side, "flip"), dict(row))
        seats["called_d"].append(cd)
        seats["called_r"].append(cr)
        seats["all"].append(round(sum_all, 1))
        seats["kalshi"].append(round(sum_kal, 1))
        seats["shown"].append(round(sum_shown, 1) if t >= secs("2026-11-04T03:28:00Z") else None)
        for src in pos:
            rows = sorted(tick_rows[src], key=lambda x: (-x[5], -x[4]))
            board_ticks[src].append([x for x in rows if x[5]] + [x for x in rows if not x[5]][:CAP])
            flips_live[src].append(sum(x[5] for x in rows))
            # value every position opened so far
            tot = collections.Counter()
            for (rid, side, kind), pp in pos[src].items():
                tl = tally_at(rid, t)
                call = side_of_call(tl)
                if call:
                    v = 1.0 if call == side else 0.0
                else:
                    b = prices.book(rid, t).get(side)
                    v = b["bid"] if b and b.get("bid") is not None else pp.get("v", pp["ask"])
                pp["v"] = v
                gain = v - pp["ask"] - pp["fee"]
                if kind == "flip":
                    tot["flip"] += gain
                else:
                    tot["board"] += gain
                    for key, _, lo, hi in BANDS:
                        if lo <= pp["ask"] < hi:
                            tot[key] += gain
            for k in curves[src]:
                curves[src][k].append(round(tot[k], 2))
        print(iso(t), "seats", seats["all"][-1], seats["kalshi"][-1], cd, cr, "open", len(pos["all"]), len(pos["shown"]), flush=True)

    # how each finished: NYT's call now, else Kalshi's settlement, else open at the last bid
    signals, table = [], {}
    for src in pos:
        stats = {}
        for (rid, side, kind), pp in pos[src].items():
            call = (live.get(rid) or {}).get("call")
            won = call == side if call in ("D", "R") else prices.settled(rid, side)
            pp["won"] = won
            signals.append(dict(src=src, kind=kind, k=pp["k"], side=side, name=pp["name"], t=iso(pp["t"]), ask=round(pp["ask"], 3), ours=round(pp["prob"], 3),
                                edge=round(pp["edge"], 3), pct=round(pp["pct"], 1), m=None if pp["m"] is None else round(pp["m"], 1), won=won, now=round(pp.get("v", pp["ask"]), 3)))
        def agg(sel):
            done = [p for p in sel if p["won"] is not None]
            op = [p for p in sel if p["won"] is None]
            return dict(n=len(sel), settled=len(done), won=sum(p["won"] for p in done), cost=round(sum(p["ask"] + p["fee"] for p in done), 2),
                        pnl=round(sum((1 if p["won"] else 0) - p["ask"] - p["fee"] for p in done), 2), open=len(op),
                        open_cost=round(sum(p["ask"] + p["fee"] for p in op), 2), open_mark=round(sum(p.get("v", p["ask"]) - p["ask"] - p["fee"] for p in op), 2),
                        implied=round(sum(p["ask"] for p in done), 1))
        b = [p for (rid, s, kd), p in pos[src].items() if kd == "board"]
        stats["flip"] = agg([p for (rid, s, kd), p in pos[src].items() if kd == "flip"])
        stats["board"] = agg(b)
        for key, _, lo, hi in BANDS:
            stats[key] = agg([p for p in b if lo <= p["ask"] < hi])
        table[src] = stats
    calls = collections.Counter((live.get(r["id"]) or {}).get("call") for r in races)
    data = dict(built=iso(int(time.time())), ticks=[iso(t) for t in ticks], seats=seats, curves=curves, flips=flips_live, board=board_ticks, table=table,
                signals=signals, races=[dict(k=r["k"], d=r["dl"], r=r["rl"]) for r in races], final=dict(d=calls.get("D", 0), r=calls.get("R", 0), open=435 - calls.get("D", 0) - calls.get("R", 0)))
    with open(os.path.join(HERE, "review_data.js"), "w", encoding="utf-8", newline="\n") as g:
        g.write("window.REVIEW = " + json.dumps(data, separators=(",", ":")) + ";\n")
    print(json.dumps(dict(final=data["final"], table=table), indent=0))
    print(os.path.getsize(os.path.join(HERE, "review_data.js")) // 1000, "KB")


if __name__ == "__main__":
    main()

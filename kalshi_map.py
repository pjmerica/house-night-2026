"""Kalshi's House markets for every district -> kalshi_map.json (read by night.py). Run before the night, or again to
pick up markets added later.

Pages the KXHOUSERACE series (most districts: KXHOUSERACE-PA07-26) and every open Kalshi event (the older
HOUSEPA7-26 / KXHOUSEPA07-26 events), and maps each event's markets to our race's two sides: the ticker's -D / -R suffix, else the candidate's last name, else
"Democrat" / "Republican" in the market's title. Fee multipliers from Kalshi's series list.

    py kalshi_map.py
"""
import json
import os
import re
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "house-night-2026 (github.com/pjmerica/house-night-2026)"}
EV = re.compile(r"^(?:KX)?HOUSE(?:RACE-)?([A-Z]{2})(\d{1,2}|AL)-(\d\d)$")    # HOUSEPA7-26, KXHOUSERACE-PA07-26


def get(url):
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 * (i + 1))
                continue
            raise
    raise RuntimeError(url)


def main():
    bench = json.load(open(os.path.join(HERE, "house_bench.json"), encoding="utf-8"))
    races = {r["k"]: r for r in bench["races"] if r["office"] == "H"}
    events, pages = [], 0
    for q in ("series_ticker=KXHOUSERACE&", ""):
        cursor = ""
        while True:
            d = get(f"{KALSHI}/events?{q}status=open&limit=200&with_nested_markets=true" + (f"&cursor={cursor}" if cursor else ""))
            pages += 1
            events += [e for e in d.get("events") or [] if EV.match(e["event_ticker"])]
            cursor = d.get("cursor")
            if not cursor:
                break
            time.sleep(0.1)
    seen = set()
    events = [e for e in events if not (e["event_ticker"] in seen or seen.add(e["event_ticker"]))]
    print(f"{pages} pages, {len(events)} House events")
    series = {}
    try:
        for s in get(f"{KALSHI}/series")["series"]:
            series[s["ticker"]] = float(s.get("fee_multiplier") or 1)
    except Exception as e:  # noqa: BLE001
        print("series list:", e)
    out, unmapped = {}, []
    for e in events:
        m = EV.match(e["event_ticker"])
        st, n, yy = m.groups()
        if yy not in ("26", "27"):
            continue
        key = f"{st}-{1 if n == 'AL' else int(n):02d}"
        race = races.get(key)
        if not race or race["kind"] != "ok":
            continue
        side = {}
        for mk in e.get("markets") or []:
            if mk.get("status") not in ("active", "open", "initialized"):
                continue
            t = mk["ticker"]
            txt = f"{mk.get('yes_sub_title') or ''} {mk.get('title') or ''}".lower()
            if t.endswith("-R"):                        # the ticker's party first, names only without one
                side.setdefault("R", t)
            elif t.endswith("-D"):
                side.setdefault("D", t)
            elif (race["rl"] and race["rl"].lower() in txt) or "republican" in txt:
                side.setdefault("R", t)
            elif (race["dl"] and race["dl"].lower() in txt) or "democrat" in txt:
                side.setdefault("D", t)
        if not side:
            unmapped.append(e["event_ticker"])
            continue
        # a district can have two events (HOUSEPA7-26 and KXHOUSERACE-PA07-26): keep both; night.py buys the cheaper
        out.setdefault(key, []).append(dict(side, event=e["event_ticker"], mult=series.get(e.get("series_ticker"), 1.0)))
    with open(os.path.join(HERE, "kalshi_map.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, indent=0, sort_keys=True)
    both = sum(1 for v in out.values() if any("D" in x and "R" in x for x in v))
    print(f"{len(out)} districts with a Kalshi market ({both} with both sides); unmapped: {unmapped[:10]}")


if __name__ == "__main__":
    main()

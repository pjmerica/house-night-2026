# Election night 2026: every race vs our model

A standalone page (separate from polling-agg's election-night data desk): every House, Senate and Governor race on a
3D map, each against the 2026 margin model, with a projection that updates as votes come in, and a Kalshi board for
the House races.

- `night.py` (GitHub Actions workflow "Every race live", started by hand: `gh workflow run night.yml`; one job
  runs up to 340 minutes) reads NYT's 50 state results pages every 60 s and writes `live.json` (every race now)
  and `hist.json` (each race's path through the night) to the orphan branch `live`. `index.html` reads them.
- Per race, the desk's projector v0 (polling-agg `live_project.project`) from our model's start:
  - count read = margin so far + (share still out) x g, sd = hypot((share out) x g_sd, 0.4), where g / g_sd are how
    the margin moved from that share counted to the end in the state's 2020-24 statewide races (12 states have
    that history; elsewhere g = 0 and the spread is all states');
  - projection = the count read and our model (sd 6.1) combined by precision; chance = Phi(mu / sd);
  - over/under = count read - model; shift = projection - model.
  District counts can run differently from their state's (one county reporting first), so the read is noisier for
  a district than for a statewide race; the projection leans on the model until a good share is counted.
- Louisiana's House races are its Nov 3 round (runoff Dec 12): shown with the leader, no projection. Same-party
  races (California / Washington top two) and uncontested seats have no projection.
- `build.py` makes `house_bench.js/json` (model margins from polling-agg `docs/forecast_data.js`, the late-count
  tables) and `house_geo.js` (polling-agg's real 2026 district lines). Re-run only if those change.

Local test: `py night.py --once`, then open `index.html?local=1`.

## Kalshi board (House)

`kalshi_map.py` finds Kalshi's House markets (the KXHOUSERACE series plus the older HOUSE{ST}{N}-26 events; 220
districts on 2026-11-03 night, the rest already settled) -> `kalshi_map.json`. Every pass `night.py` reads their
prices and, per race: the cheapest way to buy each side (YES on it or NO on the other), edge = our chance - ask -
Kalshi's taker fee, pay up to = the highest ask still 3c+ after the fee, and FLIP = the desk's rule (our chance 45%+,
edge 5c+, ask 8-40c, 20%+ counted) with the count read pointing the same way as the pick. Picks against the count are
tagged: that edge is our pre-election model arguing with a market that sees the votes. The House projection sees only
district totals (not which counties are out), so check NYT's district map and the live book before buying.

## County by county (House)

NYT's state pages carry only each district's total; each district also has its own file
(`results-{state}-us-house-{n}.json`) with every county (towns in New England) and its expected vote. For every
counting, uncalled House race `night.py` reads that file and runs polling-agg's projector v1 (`county_project.py`,
copied unchanged): each county's lean is its 2024 presidential two-party share (`house_bench.json` `l24`, from
`build.py`; a county split between districts uses the whole county's), re-centred so the district is tied, weighted by
NYT's expected vote, started from our model (sd 6.1). Where the units don't line up (Connecticut's towns tonight,
Alaska) the race keeps v0 on the district total, tagged on the page. Cards and the detail panel list the biggest
counties still out (share counted, votes left, 2024 lean, what the rest is expected to give).

## Late count and the record

The job runs through Dec 14 (a pass every 2 minutes; each job dispatches the next, and a cron restarts it if one dies).
The `live` branch keeps `hist.json` (each race's path: time, share counted, margin, projection, chance, Kalshi ask) and
`snapshots/` (the whole board at most every 10 minutes when it changed: both sides' prices, edges, FLIPs, counties still
out). Every district's NYT file, by county, is recorded by polling-agg's cloud job in the release `election-night-2026`.

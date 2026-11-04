# Election night 2026: every race vs our model

A standalone page (separate from polling-agg's election-night data desk, no markets): every House, Senate and
Governor race on a 3D map, each against the 2026 margin model, with a projection that updates as votes come in.

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

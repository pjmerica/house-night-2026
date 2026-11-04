"""Projector v1: project the count county by county.

v0 sees one number per state (share counted, visible margin). v1 sees every county: how many votes it
still has to count, and how its counted votes compare with its benchmark. That is what told Michigan
(Detroit's absentees still out) from Pennsylvania (not enough left in Philadelphia) in 2024.

For one race at one moment:
  1. Each county's lean L_c: its two-party Democratic (non-Republican) share on the log-odds scale at
     a tied statewide result (county_benchmarks.py's method: recent races averaged, uniform swing).
  2. Each county that has counted votes implies a statewide shift x_c = logit(counted share) - L_c.
     Weighted by votes counted, discounted while the county is still early in its count (its first
     batches are one kind of ballot), they give the statewide shift d and its uncertainty.
  3. A starting point (the markets' price at poll close, or our model) is a prior on d.
  4. Each county's uncounted votes (NYT's expected vote minus votes counted) are projected at
     expit(L_c + d + k_c * e_c): the statewide shift plus a share k_c of the county's own deviation
     e_c = x_c - d, trusted more as the county counts more.
  5. Simulated thousands of times with the shift's uncertainty, county errors and turnout errors
     (NYT's expected vote misses) -> projected final margin and P(the non-Republican wins).

Margins are non-Republican minus Republican in points of all votes (the two-party margin times the
two-party share of the counted vote). Parameters (PARAMS) are set by the replay in county_replay.py.
"""
import math

import numpy as np

# Set on the 2020 replay (county_replay.py, 2026-10-29): 80% bands hold 81%; leave-one-state-out the held-out
# miss is 3.4 pts vs 7.2 for the visible margin. A county's weight grows as (share counted)^6 from zero:
# half-counted counties are mostly one kind of ballot (2020 Wayne: Election Day first, absentees last).
# remaining_sd: 2020 alone (its pandemic mail swung late counts hard) preferred 0.45, but at 0.45 every 2022
# result fell inside the 80% band and in 2024 v1 overpriced longshots (Brier .058 vs the market's .045;
# .047 at 0.3), so 0.3.
PARAMS = dict(
    early_discount=0.0,     # weight of a county's counted votes when it has counted ~0% (1.0 at 100%)
    frac_power=6.0,         # ...rising as (share counted)^frac_power
    county_trust=4000,      # counted votes at which a county's own deviation gets half weight
    shift_floor=0.03,       # log-odds: floor on the statewide shift's standard error
    shift_inflate=3.0,      # counties report in a non-random order: inflate the shift's standard error
    county_sd=0.15,         # log-odds: error of a county's uncounted votes around its projection
    remaining_sd=0.3,       # log-odds: error shared by ALL uncounted votes - late ballots as a class (mail vs
                            # Election Day) differ from their counties' benchmarks together; independent county
                            # errors cancel out and left the 2020 replay's bands far too narrow
    turnout_sd=0.06,        # county expected-vote error (share)
    turnout_common_sd=0.03, # statewide expected-vote error (share)
    prior_sd=0.12,          # log-odds: starting point's uncertainty (about 6 points of margin)
    sims=4000,
)


def logit(p):
    p = min(max(p, 0.005), 0.995)
    return math.log(p / (1 - p))


def expit(x):
    return 1 / (1 + np.exp(-x))


def solve_shift(L, w, target):
    """Log-odds shift s with sum_c w_c * expit(L_c + s) = target (w sums to 1)."""
    keys = list(w)
    Lv = np.array([L[k] for k in keys])
    wv = np.array([w[k] for k in keys])
    lo, hi = -6.0, 6.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if float(np.sum(wv * expit(Lv + mid))) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def prior_shift(L, w, p_win, prior_margin_sd=6.0):
    """A starting P(non-Republican wins) -> a statewide log-odds shift: the two-party margin that
    wins with that probability under a 6-point spread, then the uniform shift that produces it."""
    from statistics import NormalDist
    m = prior_margin_sd * NormalDist().inv_cdf(min(max(p_win, 1e-4), 1 - 1e-4))
    return solve_shift(L, w, 0.5 + m / 200)


def margin_shift(L, w, m_two, sd_two):
    """A starting two-party margin (points) and its standard error -> (log-odds shift, its standard error)."""
    s0 = solve_shift(L, w, 0.5 + m_two / 200)
    hi = solve_shift(L, w, min(0.5 + (m_two + sd_two) / 200, 0.995))
    lo = solve_shift(L, w, max(0.5 + (m_two - sd_two) / 200, 0.005))
    return s0, (hi - lo) / 2


def project(units, L, w, p0=None, params=None, rng=None, m0=None):
    """units: {county: dict(d, r, total, expected)} - counted votes for the two sides, all votes counted,
    NYT's expected total. L: {county: lean}; w: {county: turnout share} (sums to 1). p0: starting
    P(non-Republican wins) or None. m0: starting point as (final margin, its sd) in points of all votes
    (our margin model), used instead of p0.
    -> dict(mu, sd, p_win, shift, shift_se, counted_pct, n_counties)."""
    P = dict(PARAMS, **(params or {}))
    rng = rng or np.random.default_rng(0)
    keys = [k for k in units if k in L]
    if not keys:
        return None
    d = np.array([units[k]["d"] for k in keys], float)
    r = np.array([units[k]["r"] for k in keys], float)
    tot = np.array([units[k]["total"] for k in keys], float)
    exp_ = np.array([max(units[k].get("expected") or 0, units[k]["total"]) for k in keys], float)
    Lv = np.array([L[k] for k in keys])
    # counties without an expected vote yet: their benchmark share of the statewide expected total
    known = exp_ > 0
    if (~known).any() and known.any():
        wv = np.array([w.get(k, 0) for k in keys])
        scale = exp_[known].sum() / max(wv[known].sum(), 1e-9)
        exp_[~known] = wv[~known] * scale
    two = d + r
    frac = np.where(exp_ > 0, np.clip(tot / np.maximum(exp_, 1), 0, 1), 0)

    # 2. the statewide shift from counties that have counted
    has = two >= 50
    x = np.where(has, np.log(np.maximum(d, 0.5) / np.maximum(r, 0.5)) - Lv, 0.0)
    fp = frac ** P["frac_power"]
    wt = np.where(has, two * (P["early_discount"] + (1 - P["early_discount"]) * fp), 0.0)
    if wt.sum() > 0:
        shift = float(np.sum(wt * x) / wt.sum())
        resid = x - shift
        sd_c = math.sqrt(float(np.sum(wt * resid ** 2) / wt.sum())) if has.sum() > 1 else 0.3
        n_eff = wt.sum() ** 2 / float(np.sum(wt ** 2))
        se = max(P["shift_floor"], P["shift_inflate"] * sd_c / math.sqrt(n_eff))
    else:
        shift, se = 0.0, 10.0
    count_shift, count_se = shift, se          # what the counted votes alone say (the dashboard's tracking)
    # 3. starting point
    s0 = None
    if m0 is not None:
        ts = two.sum() / max(tot.sum(), 1) if two.sum() >= 1000 else 0.97     # all-vote -> two-party margin
        s0, s0_sd = margin_shift(L, w, m0[0] / ts, m0[1] / ts)
    elif p0 is not None:
        s0, s0_sd = prior_shift(L, w, p0), P["prior_sd"]
    if s0 is not None:
        a, b = 1 / se ** 2, 1 / s0_sd ** 2
        shift, se = (a * shift + b * s0) / (a + b), math.sqrt(1 / (a + b))

    # 4-5. uncounted votes, simulated
    k = two / (two + P["county_trust"]) * fp
    own = np.where(has, k * (x - shift), 0.0)
    rem = np.maximum(exp_ - tot, 0)
    two_share = two.sum() / max(tot.sum(), 1) if tot.sum() > 0 else 0.97
    S = P["sims"]
    dz = rng.normal(shift, se, size=(S, 1))
    ce = rng.normal(0, P["county_sd"], size=(S, len(keys)))
    tm = (1 + rng.normal(0, P["turnout_common_sd"], size=(S, 1))) * \
         (1 + rng.normal(0, P["turnout_sd"], size=(S, len(keys))))
    rem_s = np.maximum(exp_ * tm - tot, 0)                 # expected vote can be wrong either way
    common = rng.normal(0, P["remaining_sd"], size=(S, 1))
    share = expit(Lv + dz + own + ce + common)
    rem_two = rem_s * two_share
    fd = d.sum() + np.sum(rem_two * share, axis=1)
    fr = r.sum() + np.sum(rem_two * (1 - share), axis=1)
    margin = 100 * (fd - fr) / (fd + fr) * two_share
    return dict(mu=float(margin.mean()), sd=float(margin.std()), p_win=float((margin > 0).mean()),
                shift=shift, shift_se=se, count_shift=count_shift, count_shift_se=count_se,
                two_share=float(two_share), counted_pct=float(100 * tot.sum() / max(exp_.sum(), 1)),
                remaining=float(rem.sum()), n_counties=int(has.sum()))


def shift_margin(L, w, s, two_share=0.97):
    """A uniform log-odds shift -> the statewide margin it produces, points of all votes."""
    keys = [k for k in w if k in L]
    share = float(np.sum(np.array([w[k] for k in keys]) * expit(np.array([L[k] for k in keys]) + s)))
    return 100 * (2 * share - 1) * two_share

"""Odds conversion and de-vigging.

Copied from the Sharp Karaine reference implementation (app/odds.py) and
adapted: The Odds API already returns decimal odds, so the American-odds
converter is gone. The power de-vig and the margin columns are unchanged, so
fair probabilities and margins are computed exactly as in the old tool.

Exchanges (Betfair, Matchbook, Smarkets) quote prices before their commission,
so their margin can be near 0 or negative; power_devig handles a total below 1.
"""
from __future__ import annotations


def implied_prob(decimal_odds: float) -> float:
    if decimal_odds <= 1.0:
        raise ValueError("Decimal odds must be > 1.0")
    return 1.0 / decimal_odds


def power_devig(implied_probs: list[float], tol: float = 1e-10, max_iter: int = 200) -> list[float]:
    """Power method de-vig.

    Models each raw implied probability as r_i = p_i ** (1/k) for a single
    market-wide exponent k >= 1, where p_i is the true (fair) probability.
    Equivalently p_i = r_i ** k. Solve for k via bisection such that
    sum(r_i ** k) == 1, then return [r_i ** k for r_i in implied_probs].

    This is the standard "power" de-vig method, distinct from Shin's method
    (which instead models a fraction of informed/insider money). Flagging
    the exact formula here since "power devig" alone is ambiguous - if your
    prior build used a different variant, the fair probabilities here may
    not exactly match and the displacement thresholds should be re-checked.
    """
    if not implied_probs:
        return []
    if any(p <= 0 or p >= 1 for p in implied_probs):
        raise ValueError("Implied probabilities must be in (0, 1)")

    total = sum(implied_probs)
    if abs(total - 1.0) < tol:
        return list(implied_probs)

    # sum(r_i ** k) is strictly decreasing in k for k > 0 (since 0 < r_i < 1).
    # total > 1 (overround) => need k > 1 to shrink the sum down to 1.
    # total < 1 (rare, e.g. mid-tranche crossed prices) => need k < 1.
    lo, hi = (1.0, 50.0) if total > 1.0 else (0.02, 1.0)

    def f(k: float) -> float:
        return sum(p**k for p in implied_probs) - 1.0

    f_lo, f_hi = f(lo), f(hi)
    # Expand the bracket if needed rather than assuming it always holds.
    while f_lo * f_hi > 0 and hi < 1000:
        hi *= 2
        f_hi = f(hi)

    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        f_mid = f(mid)
        if abs(f_mid) < tol:
            lo = hi = mid
            break
        if (f_lo > 0) == (f_mid > 0):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid

    k = (lo + hi) / 2.0
    return [p**k for p in implied_probs]


def fair_probs_from_decimal_odds(decimal_odds: list[float]) -> list[float]:
    raw = [implied_prob(o) for o in decimal_odds]
    return power_devig(raw)


MARGIN_COLUMNS = [
    "margin_total_pct",
    "margin_home_pp",
    "margin_draw_pp",
    "margin_away_pp",
    "margin_home_share_pct",
    "margin_draw_share_pct",
    "margin_away_share_pct",
]


def margin_columns(home_price, draw_price, away_price, fair_home, fair_draw, fair_away) -> dict:
    """One bookmaker's margin for one snapshot, in the export's home/draw/away slots
    (Over = home slot, Under = away slot for total markets, as stored).

    margin_total_pct: (sum of 1/price - 1) * 100.
    margin_<pick>_pp: that pick's price-implied probability minus its stored
    de-vigged fair probability, in percentage points; the picks sum to the total.
    margin_<pick>_share_pct: that pick's slice of the total margin.
    Two-way markets (no draw price) leave the draw columns empty. The split is
    fixed by the prices through the power de-vig used for the fair probabilities.
    """
    out = {c: None for c in MARGIN_COLUMNS}
    three_way = draw_price is not None
    prices = [home_price, draw_price, away_price] if three_way else [home_price, away_price]
    fairs = [fair_home, fair_draw, fair_away] if three_way else [fair_home, fair_away]
    if any(p is None for p in prices) or any(f is None for f in fairs):
        return out
    prices = [float(p) for p in prices]
    fairs = [float(f) for f in fairs]
    if any(p <= 1.0 for p in prices):
        return out
    imp = [1.0 / p for p in prices]
    total = sum(imp) - 1.0
    names = ["home", "draw", "away"] if three_way else ["home", "away"]
    out["margin_total_pct"] = round(total * 100.0, 6)
    for name, i, f in zip(names, imp, fairs):
        out[f"margin_{name}_pp"] = round((i - f) * 100.0, 6)
        if total > 1e-9:
            out[f"margin_{name}_share_pct"] = round((i - f) / total * 100.0, 6)
    return out

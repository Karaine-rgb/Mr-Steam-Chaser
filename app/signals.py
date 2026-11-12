"""Movement readings, computed on demand from stored snapshots.

COPIED from the predecessor tool's app/signals.py: _zigzag_moves,
velocity_shape, move_spans and steam_moves, with the same CONFIG values. Rows
passed in use that tool's field names (captured_at, fair_home_prob,
fair_draw_prob, fair_away_prob); app/analysis.py maps snapshots to them.

Docstrings kept from the old file mention RESEARCH_FINDINGS.md and
x2_displacement(); both live in the predecessor tool's repository, not here.

Changes, all deliberate:
  1. Coarse data. velocity_shape() refuses to judge a window when the gap
     between the last two snapshots is longer than that window, or when the
     snapshot it measures from lies clearly before the window start (more
     than 25 % beyond it), and labels it "window_shorter_than_gap" instead of
     stretching the window over the gap. With snapshots hours apart, most windows far from kickoff are
     skipped this way. That is the honest outcome.
  2. favorite_strength() is NOT the old one. The old engine favourite came from
     the opening Asian Handicap line; this tool collects 1X2 (h2h) only by
     default, so the favourite here is the opening 1X2 fair favourite (home or
     away) of the reference series. The lead-change override is kept: its
     backtest was purely 1X2-internal. The confirming-move confidence boost is
     dropped: it was measured against the opening AH favourite, so reusing it
     would be untested. None of these readings has been shown to predict
     results (the old backtest found steam gives no reliable edge even with
     15-minute data).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# Same values as the predecessor tool (only the keys these functions use).
CONFIG = {
    "velocity_floor_pp": 0.30,
    "move_span_min_pp": 0.80,
    "late_steam_hours": 3.0,
    "velocity_window_hours": 3.0,
    "velocity_min_window_hours": 0.25,
    "velocity_steam_ratio": 0.70,
    "velocity_drift_ratio": 0.20,
    "lead_change_min_gap_pp": 2.0,
    # Sharp Odds: skip windows the snapshots cannot resolve (see module
    # docstring). Only ever switched off to run the predecessor's own tests.
    "strict_windows": True,
}


def velocity_shape(moneyline_series: list[dict], side: str | None = None) -> dict:
    """Classifies HOW a 1X2 move happened over time, not just how big it
    got: a fast move concentrated in the recent window ("steam"), an old
    move that already happened and has since gone quiet ("drift"), a move
    still actively accumulating ("building"), one now heading back the
    other way ("reversal"), or one that swung away from the opening line
    and came most of the way back by the time we're checking ("swung_back").

    WATCH-ONLY - not wired into ah_score, x2_score, or the tier badge. The
    dashboard no longer shows it as a card: move_spans() below replays this
    classification per move and the shapes colour the 1X2 probability chart.
    Backtested against 2 real MJP rounds (32 matches, one confirmed non-
    jackpot test fixture excluded) on the AH line first: that line only
    ever moved in single atomic 0.25pt jumps, giving this classification
    nothing to actually distinguish. Re-run on the 1X2 series instead
    (real, continuous movement, not quantized steps), across 3h/6h/12h
    windows: "steam" and "building" calls were right about 70-100% of the
    time, "drift" calls only about 20-50% - a real, consistent pattern
    across all three windows tried. But the samples behind each label
    were tiny (as few as 2-5 decisive matches per label per window) -
    promising, not proven. Surfaced on the dashboard so it can keep being
    checked against real outcomes as more rounds come in, exactly like
    every other number in this file that started as a plain guess.

    Three known gaps, found and fixed after the first version shipped:
    - The look-back window used to be fixed (e.g. always 3h), which meant
      any match tracked for under 2x that window got no read at all,
      however big a move happened. It now shrinks to fit whatever history
      actually exists (down to `velocity_min_window_hours`), so an early
      real move still gets classified, just against a shorter recent
      slice - and if there genuinely isn't a usable window yet, it still
      honestly says "insufficient_history" rather than guessing.
    - A big swing that fully reverts by the time of the latest snapshot
      used to be invisible: opening-vs-current would show ~0 change, so
      it was called "quiet" even though something real happened along the
      way. Now the single largest deviation from the opening value,
      anywhere in the whole series, is tracked too - if that peak cleared
      the floor even though the net change didn't, it's labelled
      "swung_back" instead of being silently absorbed into "quiet".
    - It used to only ever compare home vs away, the same flaw
      x2_displacement() had: draw could never be the side a move was
      measured on, and the result never said which side (home/draw/away)
      the move was even about - a "steam" label alone didn't say steam on
      what. Both are fixed the same way: all three pp changes are
      computed, the one with the most POSITIVE change is the side the
      move is about (see x2_displacement()'s docstring for why "most
      positive" is the right test), and that side is now returned as
      `result["side"]`.

    `side` forces the side to classify instead of picking the most positive
    one; move_spans() uses it to read the shape of a swing on a specific line.
    """
    c = CONFIG
    configured_window = c["velocity_window_hours"]
    min_window = c["velocity_min_window_hours"]
    floor = c["velocity_floor_pp"]
    result = {
        "label": None,
        "side": None,
        "total_change_pp": 0.0,
        "recent_change_pp": None,
        "peak_change_pp": None,
        "window_hours": configured_window,
    }

    valid = [
        s for s in moneyline_series
        if s.get("fair_home_prob") is not None and s.get("fair_draw_prob") is not None
        and s.get("fair_away_prob") is not None and s.get("status") != "suspended"
    ]
    if len(valid) < 2:
        return result

    opening, current = valid[0], valid[-1]
    candidates = {
        outcome: (current[f"fair_{outcome}_prob"] - opening[f"fair_{outcome}_prob"]) * 100.0
        for outcome in ("home", "draw", "away")
    }
    if side is None:
        side = max(candidates, key=lambda k: candidates[k])
    field = f"fair_{side}_prob"
    total_change = candidates[side]
    result["side"] = side
    result["total_change_pp"] = total_change

    opening_val = opening[field]
    peak_point = max(valid, key=lambda s: abs((s[field] - opening_val) * 100.0))
    peak_change = (peak_point[field] - opening_val) * 100.0
    result["peak_change_pp"] = peak_change

    if abs(total_change) < floor:
        result["label"] = "swung_back" if abs(peak_change) >= floor else "quiet"
        return result

    last_dt, first_dt = current["captured_at"], opening["captured_at"]
    total_history_hours = (last_dt - first_dt).total_seconds() / 3600.0
    effective_window = min(configured_window, total_history_hours / 2.0)
    if effective_window < min_window:
        result["label"] = "insufficient_history"
        return result
    result["window_hours"] = effective_window

    # Sharp Odds addition (coarse data): if the last two snapshots are further
    # apart than the window being judged, the window cannot be measured.
    last_gap_hours = (last_dt - valid[-2]["captured_at"]).total_seconds() / 3600.0
    result["last_gap_hours"] = last_gap_hours
    if c["strict_windows"] and last_gap_hours > effective_window:
        result["label"] = "window_shorter_than_gap"
        return result

    cutoff = last_dt - timedelta(hours=effective_window)
    older = [s for s in valid if s["captured_at"] <= cutoff]
    if not older:
        result["label"] = "insufficient_history"
        return result

    baseline = older[-1]
    # Sharp Odds addition (coarse data): the "recent" change is really measured
    # from the baseline snapshot, which can sit far before the window start
    # when snapshots are hours apart. If the measured span is clearly longer
    # than the window (more than 25 %, at least 15 min), the move cannot be
    # placed inside the window: skip instead of calling it fast.
    measured = (last_dt - baseline["captured_at"]).total_seconds() / 3600.0
    result["measured_window_hours"] = measured
    if c["strict_windows"] and measured > effective_window + max(0.25, 0.25 * effective_window):
        result["label"] = "window_shorter_than_gap"
        return result
    recent_change = (current[field] - baseline[field]) * 100.0
    result["recent_change_pp"] = recent_change

    if (recent_change > 0) != (total_change > 0) and abs(recent_change) >= floor:
        result["label"] = "reversal"
        return result

    ratio = abs(recent_change) / abs(total_change) if total_change else 0.0
    if ratio >= c["velocity_steam_ratio"]:
        result["label"] = "steam"
    elif ratio <= c["velocity_drift_ratio"]:
        result["label"] = "drift"
    else:
        result["label"] = "building"
    return result


MOVE_SHAPES = ("steam", "building", "drift", "reversal", "swung_back")


def _zigzag_pivot_indices(values: list[float], min_reversal_frac: float = 0.001) -> list[int]:
    """Indices of the zigzag pivots of a chronological series - the same
    swings as _zigzag_moves(), but returning where they happen."""
    n = len(values)
    if n < 2:
        return []
    pivots = [0]
    direction = 0
    ext = 0
    for i in range(1, n):
        v = values[i]
        if direction == 0:
            if v > values[0]:
                direction, ext = 1, i
            elif v < values[0]:
                direction, ext = -1, i
            continue
        if direction == 1:
            if v >= values[ext]:
                ext = i
            elif values[ext] - v >= min_reversal_frac:
                pivots.append(ext)
                direction, ext = -1, i
        else:
            if v <= values[ext]:
                ext = i
            elif v - values[ext] >= min_reversal_frac:
                pivots.append(ext)
                direction, ext = 1, i
    pivots.append(ext if direction != 0 else n - 1)
    return pivots


def move_spans(moneyline_series: list[dict], min_move_pp: float | None = None) -> list[dict]:
    """The individual moves on each 1X2 line, with the shape of each, for
    colouring the 1X2 fair-probability chart.

    A move is one zigzag swing of at least `min_move_pp` (default
    CONFIG["move_span_min_pp"]) on a side's fair probability, from where it
    started to where it stopped. Its shape is the label velocity_shape()
    gives for that side at the moment the swing stopped - so the chart uses
    the engine's own definitions (steam, building, drift, reversal, swung
    back), applied to each move as it ended instead of only to the match's
    latest state. A swing where the side already sits clearly below its
    opening level is skipped (that is money leaving, not a move onto the
    side), as is any swing the classifier calls quiet or has no history for;
    those stretches keep the line's normal colour.

    Returns [{"from", "to", "side", "label", "change_pp"}] sorted by start.
    A pure function of the series, so it works for old matches too.
    """
    min_move = CONFIG["move_span_min_pp"] if min_move_pp is None else min_move_pp
    valid = [
        s for s in moneyline_series
        if s.get("fair_home_prob") is not None and s.get("fair_draw_prob") is not None
        and s.get("fair_away_prob") is not None and s.get("status") != "suspended"
    ]
    if len(valid) < 3:
        return []
    spans: list[dict] = []
    for side in ("home", "draw", "away"):
        values = [s[f"fair_{side}_prob"] for s in valid]
        pivots = _zigzag_pivot_indices(values)
        for a, b in zip(pivots, pivots[1:]):
            change = (values[b] - values[a]) * 100.0
            if abs(change) < min_move:
                continue
            r = velocity_shape(valid[: b + 1], side=side)
            if r["label"] not in MOVE_SHAPES or r["total_change_pp"] < -CONFIG["velocity_floor_pp"]:
                continue
            # the swing's pivot can sit at the end of a flat stretch's start;
            # the move really began where the line last stood still before going
            start = a
            while start + 1 < b and (values[start + 1] - values[start]) * (1 if change > 0 else -1) <= 1e-9:
                start += 1
            spans.append({
                "from": valid[start]["captured_at"],
                "to": valid[b]["captured_at"],
                "side": side,
                "label": r["label"],
                "change_pp": change,
            })
    spans.sort(key=lambda g: (g["from"], g["side"]))
    return spans


def steam_moves(moneyline_series: list[dict], kickoff: datetime | None = None) -> list[dict]:
    """Every side that has had a steam move at any moment, ranked by the size
    of its biggest steam move (largest first).

    Built from move_spans(): a steam move is one swing of at least
    CONFIG["move_span_min_pp"] on a side's fair probability that
    velocity_shape() reads as steam when it stopped. Per side this returns the
    biggest such swing (magnitude_pp, from, to) and how many steam moves the
    side has had. Sides without a steam move are not listed.

    With the match's kickoff, each swing is also labelled by when it finished:
    "late" if within CONFIG["late_steam_hours"] of kickoff, else "early". Each
    side then carries the timing of its biggest swing (timing), the biggest
    early and late swings separately (early_pp, late_pp; None if there was
    none), and every steam swing (swings, biggest first) - so the ranked
    magnitude can be traced to the swing it came from. Without a kickoff the
    timing fields are None.
    """
    late_hours = CONFIG["late_steam_hours"]

    def timing_of(end) -> tuple[str | None, float | None]:
        if kickoff is None:
            return None, None
        try:
            hours = (kickoff - end).total_seconds() / 3600.0
        except TypeError:
            return None, None
        return ("late" if hours <= late_hours else "early"), round(hours, 2)

    swings: dict[str, list[dict]] = {}
    for span in move_spans(moneyline_series):
        if span["label"] != "steam":
            continue
        timing, hours = timing_of(span["to"])
        swings.setdefault(span["side"], []).append({
            "magnitude_pp": abs(span["change_pp"]), "change_pp": span["change_pp"],
            "from": span["from"], "to": span["to"],
            "timing": timing, "hours_before_kickoff": hours,
        })
    out = []
    for side, items in swings.items():
        items.sort(key=lambda x: x["magnitude_pp"], reverse=True)
        best = items[0]
        biggest = lambda timing: next((x["magnitude_pp"] for x in items if x["timing"] == timing), None)
        out.append({
            "side": side, "magnitude_pp": best["magnitude_pp"], "count": len(items),
            "from": best["from"], "to": best["to"],
            "timing": best["timing"], "hours_before_kickoff": best["hours_before_kickoff"],
            "early_pp": biggest("early"), "late_pp": biggest("late"),
            "swings": items,
        })
    return sorted(out, key=lambda e: e["magnitude_pp"], reverse=True)


def favorite_strength(series: list[dict]) -> dict:
    """Engine favourite from the OPENING 1X2 fair probabilities (home vs away)
    of the reference series, with the lead-change override: if the other side
    leads by at least CONFIG["lead_change_min_gap_pp"] at the latest reading,
    it becomes the favourite. See the module docstring for how this differs
    from the predecessor tool."""
    valid = [s for s in series if s.get("fair_home_prob") is not None and s.get("fair_away_prob") is not None]
    out = {"favorite": None, "opening_favorite": None, "favorite_prob": None,
           "lead_change": False, "lead_change_gap_pp": None}
    if not valid:
        return out
    first, last = valid[0], valid[-1]
    if first["fair_home_prob"] == first["fair_away_prob"]:
        return out  # pick'em: no pick
    opening = "home" if first["fair_home_prob"] > first["fair_away_prob"] else "away"
    out["opening_favorite"] = out["favorite"] = opening
    out["favorite_prob"] = first[f"fair_{opening}_prob"] * 100.0
    leader = "home" if last["fair_home_prob"] > last["fair_away_prob"] else "away"
    gap = abs(last["fair_home_prob"] - last["fair_away_prob"]) * 100.0
    if leader != opening and gap >= CONFIG["lead_change_min_gap_pp"]:
        out.update(favorite=leader, favorite_prob=last[f"fair_{leader}_prob"] * 100.0,
                   lead_change=True, lead_change_gap_pp=gap)
    return out

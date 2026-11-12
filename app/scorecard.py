"""How often the engine's favourite, confirmed steam and the two together were right.

result_side() and tally() are COPIED unchanged from the predecessor tool, so
the counting (including the chance-level comparison for steam covering
several sides) is the same. What feeds them differs:
  - Engine pick: app.signals.favorite_strength() on the reference series
    (opening 1X2 favourite with the lead-change override; NOT the old AH-based
    favourite, so the old backtest numbers do not carry over).
  - Steam sides: sides whose steam is CONFIRMED in at least two bookmakers
    (app/analysis.py). On coarse data most windows are skipped, so expect
    few flagged matches.
Nothing is stored; it is recomputed from snapshots and entered scores.
"""
from __future__ import annotations


def result_side(home_score, away_score) -> str | None:
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "home"
    if away_score > home_score:
        return "away"
    return "draw"



def read_match(analysis: dict) -> dict:
    """{"engine": favourite side or None, "steam_sides": [confirmed steam sides]}"""
    return {
        "engine": analysis["engine"].get("favorite"),
        "steam_sides": [s["side"] for s in analysis["steam"]["sides"] if s["confirmed"]],
    }


def tally(matches: list[dict]) -> dict:
    """matches: [{"result": side, "engine": side|None, "steam_sides": [..]}] (settled matches only)."""
    engine_picks = engine_right = 0
    steam_flagged = steam_right = steam_sides_total = 0
    conv = conv_right = 0
    for m in matches:
        res, pick, sides = m["result"], m["engine"], set(m["steam_sides"])
        if pick is not None:
            engine_picks += 1
            engine_right += pick == res
        if sides:
            steam_flagged += 1
            steam_sides_total += len(sides)
            steam_right += res in sides
        if pick is not None and pick in sides:
            conv += 1
            conv_right += pick == res
    return {
        "settled": len(matches),
        "engine": {"picks": engine_picks, "right": int(engine_right), "no_pick": len(matches) - engine_picks},
        "steam": {
            "flagged": steam_flagged,
            "right": int(steam_right),
            "no_steam": len(matches) - steam_flagged,
            "avg_sides": round(steam_sides_total / steam_flagged, 2) if steam_flagged else None,
            # hits a blind pick of the same NUMBER of sides (any sides, equally likely) would get on average
            "chance_right": round(steam_sides_total / 3.0, 1),
        },
        "converged": {"count": conv, "right": int(conv_right)},
    }

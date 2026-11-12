"""Load a round's matches with their snapshots and polls, and analyse them."""
from __future__ import annotations

from collections import defaultdict

from app.analysis import analyse_match


async def load_matches(pool, round_label: str | None = None, matchup_id: int | None = None) -> list[dict]:
    async with pool.acquire() as con:
        matches = await con.fetch(
            "select * from matchups where round_label is distinct from '__deleting__' "
            "and ($1::text is null or round_label = $1) and ($2::bigint is null or id = $2) "
            "order by commence_time, id", round_label, matchup_id)
        ids = [m["id"] for m in matches]
        snaps = await con.fetch(
            "select matchup_id, bookmaker, market, captured_at, home_price, draw_price, away_price, point, "
            "fair_home, fair_draw, fair_away from odds_snapshots where matchup_id = any($1::bigint[]) "
            "and market = 'h2h' order by captured_at, id", ids)
        polls = await con.fetch("select matchup_id, captured_at, bookmakers from match_polls "
                                "where matchup_id = any($1::bigint[]) order by captured_at", ids)
    by_snap, by_poll = defaultdict(list), defaultdict(list)
    for s in snaps:
        by_snap[s["matchup_id"]].append(dict(s))
    for p in polls:
        by_poll[p["matchup_id"]].append(dict(p))
    out = []
    for m in matches:
        out.append({"match": dict(m), "analysis": analyse_match(by_snap[m["id"]], by_poll[m["id"]],
                                                                m["commence_time"])})
    return out

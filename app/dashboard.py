"""Read models for the dashboard: match summary rows, one match's detail,
storage overview. Pure reads plus the small result/monitoring writes.

Reference price for the summary: Pinnacle's latest 1X2 when Pinnacle priced
the match, otherwise the CONSENSUS = per-side median of every book's latest
fair probability, renormalised to 100 %. The row says which one it shows.
Phase 5 adds the full per-bookmaker comparison and steam readings.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import median

REFERENCE_BOOK = "pinnacle"
SIDES = ("home", "draw", "away")


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def reference_probs(latest_by_book: dict[str, dict]) -> dict | None:
    """latest_by_book: bookmaker -> row with fair_home/draw/away (h2h)."""
    books = {b: r for b, r in latest_by_book.items() if r.get("fair_home") is not None}
    if not books:
        return None
    if REFERENCE_BOOK in books:
        r = books[REFERENCE_BOOK]
        return {"source": REFERENCE_BOOK, "books": 1,
                **{s: r[f"fair_{s}"] for s in SIDES}}
    med = {s: median(r[f"fair_{s}"] for r in books.values() if r[f"fair_{s}"] is not None)
           if any(r[f"fair_{s}"] is not None for r in books.values()) else None for s in SIDES}
    total = sum(v for v in med.values() if v is not None) or 1.0
    return {"source": "consensus", "books": len(books),
            **{s: (med[s] / total if med[s] is not None else None) for s in SIDES}}


async def matches_summary(pool, round_label: str | None) -> list[dict]:
    async with pool.acquire() as con:
        matches = await con.fetch(
            "select id, event_id, sport_key, league_title, home_team, away_team, commence_time, round_label, "
            "is_monitored, home_score, away_score from matchups "
            "where round_label is distinct from '__deleting__' and ($1::text is null or round_label = $1) "
            "order by commence_time, id", round_label)
        ids = [m["id"] for m in matches]
        h2h = await con.fetch(
            "select matchup_id, bookmaker, captured_at, home_price, draw_price, away_price, "
            "fair_home, fair_draw, fair_away from odds_snapshots "
            "where matchup_id = any($1::bigint[]) and market = 'h2h' order by matchup_id, captured_at, id", ids)
        counts = {r["matchup_id"]: (r["n"], r["last"]) for r in await con.fetch(
            "select matchup_id, count(*) as n, max(captured_at) as last from odds_snapshots "
            "where matchup_id = any($1::bigint[]) group by 1", ids)}
    first: dict[int, dict] = defaultdict(dict)
    last: dict[int, dict] = defaultdict(dict)
    for r in h2h:
        d = dict(r)
        first[r["matchup_id"]].setdefault(r["bookmaker"], d)
        last[r["matchup_id"]][r["bookmaker"]] = d
    out = []
    for m in matches:
        now_ref = reference_probs(last[m["id"]])
        open_ref = reference_probs(first[m["id"]])
        delta = None
        if now_ref and open_ref and now_ref["source"] == open_ref["source"]:
            delta = {s: (round((now_ref[s] - open_ref[s]) * 100, 2)
                         if now_ref[s] is not None and open_ref[s] is not None else None) for s in SIDES}
        n, last_at = counts.get(m["id"], (0, None))
        out.append({
            "id": m["id"], "event_id": m["event_id"], "sport_key": m["sport_key"],
            "league": m["league_title"] or m["sport_key"], "home_team": m["home_team"],
            "away_team": m["away_team"], "kickoff": _iso(m["commence_time"]), "round_label": m["round_label"],
            "is_monitored": m["is_monitored"], "home_score": m["home_score"], "away_score": m["away_score"],
            "snapshots": int(n), "last_snapshot": _iso(last_at), "books": len(last[m["id"]]),
            "reference": now_ref, "delta_pp": delta,
        })
    return out


async def match_detail(pool, matchup_id: int) -> dict | None:
    async with pool.acquire() as con:
        m = await con.fetchrow("select * from matchups where id = $1 and round_label is distinct from '__deleting__'",
                               matchup_id)
        if m is None:
            return None
        snaps = await con.fetch(
            "select bookmaker, market, captured_at, market_last_update, home_price, draw_price, away_price, point, "
            "fair_home, fair_draw, fair_away from odds_snapshots where matchup_id = $1 "
            "order by captured_at, bookmaker, market, point", matchup_id)
        calls = await con.fetch(
            "select coalesce(finished_at, created_at) as t, kind, status, charged, reason from credit_ledger "
            "where is_paid and endpoint = 'odds' and sport_key = $1 and created_at >= $2 "
            "order by created_at", m["sport_key"], m["added_at"])
        settings = await con.fetchrow("select bookmakers, markets, schedule_preset from round_settings "
                                      "where round_label = $1", m["round_label"])
    match = {k: _iso(v) for k, v in dict(m).items()}
    captures = sorted({s["captured_at"] for s in snaps})
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(captures, captures[1:])]
    return {
        "match": match,
        "settings": dict(settings) if settings else None,
        "snapshots": [{k: _iso(v) for k, v in dict(s).items()} for s in snaps],
        "polls": [{k: _iso(v) for k, v in dict(c).items()} for c in calls],
        "capture_times": [_iso(c) for c in captures],
        "gap_minutes": {"min": round(min(gaps)) if gaps else None, "max": round(max(gaps)) if gaps else None,
                        "median": round(median(gaps)) if gaps else None},
    }


async def set_result(pool, matchup_id: int, home: int | None, away: int | None) -> bool:
    if (home is None) != (away is None):
        raise ValueError("enter both scores, or clear both")
    if home is not None and not (0 <= home <= 99 and 0 <= away <= 99):
        raise ValueError("scores must be between 0 and 99")
    async with pool.acquire() as con:
        res = await con.execute("update matchups set home_score = $1, away_score = $2 where id = $3",
                                home, away, matchup_id)
    return res.endswith(" 1")


async def set_monitored(pool, matchup_id: int, on: bool, now: datetime) -> bool:
    async with pool.acquire() as con:
        if on:
            res = await con.execute("update matchups set is_monitored = true where id = $1 and commence_time > $2 "
                                    "and round_label is distinct from '__deleting__'", matchup_id, now)
        else:
            res = await con.execute("update matchups set is_monitored = false where id = $1", matchup_id)
    return res.endswith(" 1")


async def storage_overview(pool, guard) -> dict:
    async with pool.acquire() as con:
        db_bytes = int(await con.fetchval("select pg_database_size(current_database())"))
        tables = await con.fetch(
            "select c.relname as name, pg_total_relation_size(c.oid) as bytes from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' and c.relkind = 'r' "
            "order by 2 desc")
        rounds = await con.fetch(
            "select m.round_label, count(distinct m.id) as matches, count(s.id) as snapshots, "
            "coalesce(bool_or(m.is_monitored), false) as active, min(m.commence_time) as first_kickoff, "
            "max(d.downloaded_at) as downloaded_at, max(d.snapshot_count) as downloaded_snapshots "
            "from matchups m left join odds_snapshots s on s.matchup_id = m.id "
            "left join round_downloads d on d.round_label = m.round_label "
            "where m.round_label is not null and m.round_label <> '__deleting__' "
            "group by m.round_label order by min(m.commence_time) desc")
        pending = int(await con.fetchval("select count(*) from matchups where round_label = '__deleting__'"))
    snap_bytes = sum(int(t["bytes"]) for t in tables if t["name"] == "odds_snapshots")
    total_snaps = sum(int(r["snapshots"]) for r in rounds) or 1
    usage = await guard.usage(force=True) if guard is not None else None
    return {
        "db_bytes": db_bytes, "usage": usage, "pending_delete_matches": pending,
        "tables": [{"name": t["name"], "bytes": int(t["bytes"])} for t in tables],
        "rounds": [{**{k: _iso(v) for k, v in dict(r).items()},
                    "est_bytes": int(snap_bytes * int(r["snapshots"]) / total_snaps)} for r in rounds],
    }

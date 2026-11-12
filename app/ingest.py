"""Store parsed odds as snapshot rows, only when something changed.

De-duplication ("version diff"): for each match x bookmaker x market, the new
group of rows (usually one; two or more only if a book quotes several lines
at once) is compared with the group stored at that pair's latest capture
time. If the prices and lines are identical, nothing is written. A book that
disappears from a response writes nothing either; its last row simply stays
the latest.

Every poll also writes one match_polls row per covered match (time and
bookmakers present), so analysis can rebuild each book's full series.

Kickoff rule (same as the predecessor tool): rows for a match whose kickoff
has passed are never stored, even if the response was requested just before.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from app.parse import SnapshotRow, parse_odds_response

COLUMNS = ("matchup_id", "bookmaker", "market", "captured_at", "market_last_update", "home_price",
           "draw_price", "away_price", "point", "fair_home", "fair_draw", "fair_away", "ledger_id")


def _group_key(rows) -> tuple:
    return tuple(sorted((r[0], r[1], r[2], r[3]) for r in rows))


async def ingest_odds(pool, events: list[dict], captured_at: datetime, ledger_id: int | None) -> dict:
    """Returns counts: stored rows, unchanged groups, rows dropped for kickoff/unknown match."""
    parsed = parse_odds_response(events)
    if not parsed:
        return {"stored": 0, "unchanged": 0, "dropped": 0, "matches": 0}
    event_ids = sorted({r.event_id for r in parsed})
    async with pool.acquire() as con:
        async with con.transaction():
            matches = {r["event_id"]: r for r in await con.fetch(
                "select id, event_id, commence_time from matchups "
                "where event_id = any($1::text[]) and is_monitored", event_ids)}
            groups: dict[tuple, list[SnapshotRow]] = defaultdict(list)
            dropped = 0
            for row in parsed:
                m = matches.get(row.event_id)
                if m is None or m["commence_time"] <= captured_at:
                    dropped += 1
                    continue
                groups[(m["id"], row.bookmaker, row.market)].append(row)
            if not groups:
                return {"stored": 0, "unchanged": 0, "dropped": dropped, "matches": 0}

            ids = sorted({k[0] for k in groups})
            latest = await con.fetch(
                "select s.matchup_id, s.bookmaker, s.market, s.home_price, s.draw_price, s.away_price, s.point "
                "from odds_snapshots s join ("
                "  select matchup_id, bookmaker, market, max(captured_at) as t from odds_snapshots "
                "  where matchup_id = any($1::bigint[]) group by 1, 2, 3"
                ") last on last.matchup_id = s.matchup_id and last.bookmaker = s.bookmaker "
                "and last.market = s.market and last.t = s.captured_at", ids)
            previous: dict[tuple, list] = defaultdict(list)
            for r in latest:
                previous[(r["matchup_id"], r["bookmaker"], r["market"])].append(
                    (r["home_price"], r["draw_price"], r["away_price"], r["point"]))

            to_write, unchanged = [], 0
            for key, rows in groups.items():
                new = [r.price_key() for r in rows]
                if key in previous and _group_key(previous[key]) == _group_key(new):
                    unchanged += 1
                    continue
                for r in rows:
                    to_write.append((key[0], r.bookmaker, r.market, captured_at, r.market_last_update,
                                     r.home_price, r.draw_price, r.away_price, r.point,
                                     r.fair_home, r.fair_draw, r.fair_away, ledger_id))
            if to_write:
                await con.copy_records_to_table("odds_snapshots", records=to_write, columns=COLUMNS)
            # Record the poll itself for every match it covered, changed or not.
            seen: dict[int, set[str]] = defaultdict(set)
            for (mid, book, _market) in groups:
                seen[mid].add(book)
            await con.executemany(
                "insert into match_polls (matchup_id, captured_at, ledger_id, bookmakers) values ($1, $2, $3, $4) "
                "on conflict (matchup_id, captured_at) do nothing",
                [(mid, captured_at, ledger_id, sorted(books)) for mid, books in seen.items()])
    return {"stored": len(to_write), "unchanged": unchanged, "dropped": dropped,
            "matches": len({k[0] for k in groups})}

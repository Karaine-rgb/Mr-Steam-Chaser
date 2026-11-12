"""Snapshot storage: only changes are written; nothing after kickoff."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.ingest import ingest_odds

LIVE = Path(__file__).resolve().parent.parent / "app" / "mock_data" / "live"
T0 = datetime(2026, 11, 12, 9, 44, tzinfo=timezone.utc)  # recording time


@pytest.fixture
def epl():
    return json.loads((LIVE / "odds_soccer_epl_h2h.json").read_text())


async def track(pool, events, monitored=True):
    async with pool.acquire() as con:
        for e in events:
            await con.execute(
                "insert into matchups (event_id, sport_key, home_team, away_team, commence_time, round_label, is_monitored) "
                "values ($1, $2, $3, $4, $5, 'R', $6)", e["id"], e["sport_key"], e["home_team"], e["away_team"],
                datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")), monitored)


async def snap_count(pool):
    async with pool.acquire() as con:
        return await con.fetchval("select count(*) from odds_snapshots")


async def test_first_response_stores_everything_for_tracked_matches(pool, epl):
    await track(pool, epl[:3])
    res = await ingest_odds(pool, epl, T0, None)
    expected = sum(1 for e in epl[:3] for b in e["bookmakers"] for m in b["markets"] if m["key"] == "h2h")
    assert res["stored"] == expected and res["matches"] == 3
    assert res["dropped"] > 0  # the 17 untracked events
    assert await snap_count(pool) == expected


async def test_identical_response_stores_nothing(pool, epl):
    await track(pool, epl[:3])
    first = await ingest_odds(pool, epl, T0, None)
    again = await ingest_odds(pool, epl, T0 + timedelta(hours=1), None)
    assert again["stored"] == 0 and again["unchanged"] == first["stored"]
    assert await snap_count(pool) == first["stored"]


async def test_only_changed_bookmaker_is_written(pool, epl):
    await track(pool, epl[:1])
    await ingest_odds(pool, epl[:1], T0, None)
    changed = copy.deepcopy(epl[:1])
    pin = next(b for b in changed[0]["bookmakers"] if b["key"] == "pinnacle")
    pin["markets"][0]["outcomes"][0]["price"] += 0.05
    res = await ingest_odds(pool, changed, T0 + timedelta(hours=1), None)
    assert res["stored"] == 1
    async with pool.acquire() as con:
        rows = await con.fetch("select bookmaker, captured_at from odds_snapshots order by captured_at desc limit 1")
    assert rows[0]["bookmaker"] == "pinnacle"


async def test_missing_bookmaker_writes_nothing(pool, epl):
    await track(pool, epl[:1])
    await ingest_odds(pool, epl[:1], T0, None)
    fewer = copy.deepcopy(epl[:1])
    fewer[0]["bookmakers"] = fewer[0]["bookmakers"][:1]
    res = await ingest_odds(pool, fewer, T0 + timedelta(hours=1), None)
    assert res["stored"] == 0 and res["unchanged"] == 1


async def test_after_kickoff_nothing_is_stored(pool, epl):
    await track(pool, epl[:1])
    kickoff = datetime.fromisoformat(epl[0]["commence_time"].replace("Z", "+00:00"))
    res = await ingest_odds(pool, epl[:1], kickoff + timedelta(seconds=1), None)
    assert res["stored"] == 0 and res["dropped"] > 0
    assert await snap_count(pool) == 0


async def test_unmonitored_match_not_stored(pool, epl):
    await track(pool, epl[:1], monitored=False)
    assert (await ingest_odds(pool, epl[:1], T0, None))["stored"] == 0


def _line_event(points_prices):
    """Hand-written totals market with given (point, over, under) lines."""
    return [{"id": "f" * 32, "sport_key": "soccer_test", "commence_time": "2026-12-01T15:00:00Z",
             "home_team": "Home FC", "away_team": "Away FC", "bookmakers": [{
                 "key": "x", "title": "x", "markets": [{"key": "totals", "last_update": "2026-11-30T10:00:00Z",
                   "outcomes": [o for pt, ov, un in points_prices for o in (
                       {"name": "Over", "price": ov, "point": pt}, {"name": "Under", "price": un, "point": pt})]}]}]}]


async def test_line_move_and_multi_line_groups(pool):
    ev = _line_event([(2.5, 1.9, 1.95)])
    await track(pool, ev)
    assert (await ingest_odds(pool, ev, T0, None))["stored"] == 1
    moved = _line_event([(2.25, 1.8, 2.05)])  # quarter line, new line
    assert (await ingest_odds(pool, moved, T0 + timedelta(hours=1), None))["stored"] == 1
    two = _line_event([(2.25, 1.8, 2.05), (2.5, 1.95, 1.9)])
    assert (await ingest_odds(pool, two, T0 + timedelta(hours=2), None))["stored"] == 2
    # same two lines again, listed in the other order: unchanged
    two_swapped = _line_event([(2.5, 1.95, 1.9), (2.25, 1.8, 2.05)])
    assert (await ingest_odds(pool, two_swapped, T0 + timedelta(hours=3), None))["stored"] == 0


async def test_every_poll_is_recorded_even_when_nothing_changed(pool, epl):
    await track(pool, epl[:2])
    await ingest_odds(pool, epl, T0, None)
    await ingest_odds(pool, epl, T0 + timedelta(hours=1), None)
    async with pool.acquire() as con:
        rows = await con.fetch("select matchup_id, captured_at, bookmakers from match_polls order by captured_at")
    assert len(rows) == 4  # 2 matches x 2 polls
    assert "pinnacle" in rows[0]["bookmakers"]

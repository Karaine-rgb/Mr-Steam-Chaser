"""Poller: lease, schedule, kickoff cutoff, backoff, pauses, manual refresh.
All in mock mode on a throwaway database, with a hand-driven clock."""
from datetime import timedelta

import httpx
import pytest

from app.config import DEFAULT_BOOKMAKERS
from app.credits import CreditManager
from app.discovery import Discovery, DiscoveryCache
from app.odds_api import OddsApiClient
from app.parse import parse_time
from app.poller import Poller

BOOKS = list(DEFAULT_BOOKMAKERS)


@pytest.fixture
def setup(pool, make_manager, clock):
    mgr = make_manager()
    disc = Discovery(mgr, DiscoveryCache())
    return mgr, disc, Poller(pool, mgr, disc, clock=clock)


async def add(disc, n=3, sport="soccer_epl", label="R", preset="balanced"):
    ev = (await disc.events_for([sport]))["events"][:n]
    await disc.add_to_round(label, ev, BOOKS, ["h2h"], preset)
    return ev


async def paid(pool, **where):
    sql = "select * from credit_ledger where is_paid"
    async with pool.acquire() as con:
        rows = [dict(r) for r in await con.fetch(sql + " order by id")]
    return [r for r in rows if all(r[k] == v for k, v in where.items())]


async def snapshots(pool):
    async with pool.acquire() as con:
        return await con.fetchval("select count(*) from odds_snapshots")


async def test_idle_without_tracked_matches(setup, pool):
    _, _, poller = setup
    res = await poller.tick()
    assert res["leader"] and res["called"] is None
    assert await paid(pool) == []


async def test_opening_snapshot_then_nothing_until_next_point(setup, pool, mock_api):
    _, disc, poller = setup
    await add(disc, 3)
    first = await poller.tick()
    assert first["called"] == "soccer_epl" and first["result"]["status"] == "ok"
    assert first["result"]["ingest"]["matches"] == 3 and await snapshots(pool) > 0
    (row,) = await paid(pool)
    assert row["kind"] == "planned" and row["charged"] == 1 and "added" in row["reason"]
    assert row["events_requested"] == 3
    # the request only asked for the tracked events and never for started ones
    q = mock_api.requests[-1].url.params
    assert len(q["eventIds"].split(",")) == 3 and "commenceTimeFrom" in q
    assert (await poller.tick())["called"] is None
    assert len(await paid(pool)) == 1


async def test_simulated_week_spends_what_was_planned_and_stops_at_kickoff(setup, pool, clock):
    from collections import Counter

    from app.planner import TrackedMatch, build_plan, month_bounds
    _, disc, poller = setup
    events = (await disc.events_for(["soccer_epl"]))["events"]
    kick = Counter(e["commence_time"] for e in events).most_common(1)[0][0]
    same = [e for e in events if e["commence_time"] == kick][:3]  # one kickoff group
    await disc.add_to_round("R", same, BOOKS, ["h2h"], "balanced")
    kickoff = parse_time(kick)
    predicted = build_plan(await disc.tracked_matches(), {}, clock.now, month_bounds(clock.now)[1], 10**6)
    while clock.now < kickoff + timedelta(hours=3):
        await poller.tick()
        clock.now += timedelta(minutes=10)
    rows = await paid(pool)
    assert len(predicted.calls) == 5
    assert len(rows) == len(predicted.calls)  # spent exactly what the plan said
    assert all(r["created_at"] < kickoff for r in rows)
    async with pool.acquire() as con:
        assert await con.fetchval("select count(*) from matchups where is_monitored") == 0
        late = await con.fetchval("select count(*) from odds_snapshots s join matchups m on m.id = s.matchup_id "
                                  "where s.captured_at >= m.commence_time")
    assert late == 0


async def test_two_kickoff_groups_cost_more_but_stay_within_plan(setup, pool, clock):
    from app.planner import build_plan, month_bounds
    _, disc, poller = setup
    events = (await disc.events_for(["soccer_epl"]))["events"]
    first_day = [e for e in events if e["commence_time"][:10] == events[0]["commence_time"][:10]]
    times = sorted({e["commence_time"] for e in first_day})[:2]
    picks = [next(e for e in first_day if e["commence_time"] == t) for t in times]
    await disc.add_to_round("R", picks, BOOKS, ["h2h"], "balanced")
    predicted = build_plan(await disc.tracked_matches(), {}, clock.now, month_bounds(clock.now)[1], 10**6)
    end = max(parse_time(t) for t in times) + timedelta(hours=1)
    while clock.now < end:
        await poller.tick()
        clock.now += timedelta(minutes=10)
    assert 5 < len(await paid(pool)) <= len(predicted.calls)


async def test_only_one_leader(pool, make_manager, clock):
    mgr = make_manager()
    disc = Discovery(mgr, DiscoveryCache())
    a, b = Poller(pool, mgr, disc, clock=clock), Poller(pool, mgr, disc, clock=clock)
    assert (await a.tick())["leader"] is True
    assert (await b.tick())["leader"] is False
    clock.now += timedelta(seconds=60)
    assert (await a.tick())["leader"] is True  # renewed
    clock.now += timedelta(seconds=200)        # a stops renewing (e.g. old copy shut down)
    assert (await b.tick())["leader"] is True
    assert (await a.tick())["leader"] is False
    await b.stop()
    assert (await a.tick())["leader"] is True  # released lease is free at once


async def test_failed_calls_back_off(pool, clock):
    calls = []

    def handler(request):
        if request.url.path.endswith("/odds"):
            calls.append(request)
            raise httpx.ConnectError("down")
        from app.mock_api import MockOddsApi
        return MockOddsApi(clock=clock).handle(request)

    client = OddsApiClient("https://x", "", transport=httpx.MockTransport(handler), is_mock=True)
    mgr = CreditManager(pool, client, clock=clock)
    disc = Discovery(mgr, DiscoveryCache())
    poller = Poller(pool, mgr, disc, clock=clock)
    await add(disc, 2)
    await poller.tick()
    assert len(calls) == 1
    for _ in range(5):  # within 15 minutes: no retry
        clock.now += timedelta(minutes=2)
        res = await poller.tick()
        assert res["called"] is None and "soccer_epl" in res["waiting"]
    clock.now += timedelta(minutes=6)
    await poller.tick()
    assert len(calls) == 2
    clock.now += timedelta(minutes=20)  # second failure waits 30 min
    assert (await poller.tick())["called"] is None
    clock.now += timedelta(minutes=11)
    await poller.tick()
    assert len(calls) == 3
    await client.aclose()


async def test_storage_pause_stops_polling(pool, make_manager, clock):
    class Full:
        async def collection_blocked(self):
            return True
    mgr = make_manager()
    disc = Discovery(mgr, DiscoveryCache())
    poller = Poller(pool, mgr, disc, guard=Full(), clock=clock)
    await add(disc, 2)
    assert (await poller.tick())["skipped"] == "storage_paused"
    assert await paid(pool) == []


async def test_credit_pause_stops_polling(pool, make_manager, clock, mock_api):
    mgr = make_manager(limit=10, reserve=0, pause_pct=10)  # pause line 1
    disc = Discovery(mgr, DiscoveryCache())
    poller = Poller(pool, mgr, disc, clock=clock)
    await add(disc, 2)
    await poller.tick()  # spends the single credit
    clock.now += timedelta(days=2)
    assert (await poller.tick())["skipped"] == "credits_paused"
    assert len(await paid(pool)) == 1


async def test_two_leagues_nearest_kickoff_first(setup, pool):
    _, disc, poller = setup
    await add(disc, 2, "soccer_epl", "R1")
    await add(disc, 2, "soccer_germany_bundesliga", "R1")
    first = await poller.tick()
    second = await poller.tick()
    assert {first["called"], second["called"]} == {"soccer_epl", "soccer_germany_bundesliga"}
    assert first["called"] == "soccer_germany_bundesliga"  # Bundesliga kicks off first (Friday)


async def test_manual_refresh_needs_the_right_cost(setup, pool):
    _, disc, poller = setup
    await add(disc, 2, "soccer_epl", "Weekend")
    await add(disc, 2, "soccer_spain_la_liga", "Weekend")
    quote = await poller.round_refresh_quote("Weekend")
    assert quote["cost"] == 2 and len(quote["leagues"]) == 2
    wrong = await poller.refresh_round("Weekend", 1)
    assert wrong["ok"] is False and await paid(pool) == []
    done = await poller.refresh_round("Weekend", 2)
    assert done["ok"] and len(await paid(pool, kind="manual")) == 2


async def test_plan_for_match_lists_points(setup):
    _, disc, poller = setup
    ev = await add(disc, 1)
    await poller.tick()
    labels = [p["label"] for p in poller.plan_for(ev[0]["event_id"])]
    assert labels == ["24h", "6h", "2h", "30m"]

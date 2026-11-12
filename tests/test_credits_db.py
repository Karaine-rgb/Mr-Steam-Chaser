"""The paid-call gate against a real (throwaway) Postgres and the mock API."""
import asyncio
from datetime import timedelta

import httpx
import pytest

from app.config import DEFAULT_BOOKMAKERS
from app.credits import CreditManager
from app.odds_api import OddsApiClient

BOOKS = list(DEFAULT_BOOKMAKERS)


async def epl(mgr, kind="planned", **kw):
    return await mgr.paid_odds("soccer_epl", ["h2h"], BOOKS, kind, "test", **kw)


async def ledger(pool):
    async with pool.acquire() as con:
        return [dict(r) for r in await con.fetch("select * from credit_ledger order by id")]


async def test_paid_call_records_real_cost_from_headers(make_manager, pool, mock_api):
    mgr = make_manager()
    out = await epl(mgr)
    assert out.allowed and out.status == "ok" and len(out.events) == 20
    (row,) = await ledger(pool)
    assert row["is_paid"] and row["status"] == "ok"
    assert row["expected_cost"] == 1 and row["charged"] == 1
    assert row["used_after"] == 1 and row["remaining_after"] == 499
    assert row["events_returned"] == 20 and row["bookmakers"] == ",".join(BOOKS)
    assert (await mgr.budget()).used == 1
    assert mock_api.used == 1


async def test_refused_call_never_reaches_api(make_manager, pool, mock_api):
    mgr = make_manager(limit=10, reserve=0, pause_pct=10)  # pause line = 1
    assert (await epl(mgr)).allowed
    sent = len(mock_api.requests)
    out = await epl(mgr)
    assert not out.allowed and "paused" in out.reason
    assert len(mock_api.requests) == sent  # nothing went out
    assert len(await ledger(pool)) == 1    # refusals are not ledger rows


async def test_empty_response_costs_nothing(make_manager, pool, clock):
    mgr = make_manager()
    out = await epl(mgr, commence_from=(clock() + timedelta(days=300)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert out.status == "empty" and out.events == []
    (row,) = await ledger(pool)
    assert row["charged"] == 0
    assert (await mgr.budget()).used == 0


async def test_free_calls_are_logged_and_sync_the_counter(make_manager, pool, mock_api):
    mock_api.used = 37  # e.g. the key was also used elsewhere
    mgr = make_manager()
    b = await mgr.sync()
    assert b.used == 37 and b.api_remaining == 463
    rows = await ledger(pool)
    assert rows[0]["endpoint"] == "sports" and not rows[0]["is_paid"] and rows[0]["charged"] == 0
    await mgr.free_events("soccer_epl")
    assert (await mgr.budget()).used == 37


async def test_pending_row_counts_and_blocks_a_second_copy(make_manager, pool, clock):
    mgr = make_manager(limit=10, reserve=0, pause_pct=10)  # line = 1
    async with pool.acquire() as con:  # another app copy is mid-request
        await con.execute(
            "insert into credit_ledger (created_at, endpoint, is_paid, is_mock, kind, expected_cost, status) "
            "values ($1, 'odds', true, true, 'planned', 1, 'pending')", clock())
    b = await mgr.budget()
    assert b.used == 1 and b.uncertain == 1
    assert not (await epl(mgr)).allowed


async def test_stale_pending_becomes_unknown_and_sync_clears_it(make_manager, pool, clock, mock_api):
    mgr = make_manager()
    async with pool.acquire() as con:
        await con.execute(
            "insert into credit_ledger (created_at, endpoint, is_paid, is_mock, kind, expected_cost, status) "
            "values ($1, 'odds', true, true, 'planned', 3, 'pending')", clock() - timedelta(minutes=30))
    assert await mgr.recover_stale_pending() == 1
    assert [r["status"] for r in await ledger(pool)] == ["unknown"]
    assert (await mgr.budget()).used == 3     # counted at expected cost
    clock.now += timedelta(minutes=5)
    b = await mgr.sync()                       # API says nothing was actually charged
    assert b.used == 0 and b.uncertain == 0


async def test_recent_pending_is_not_marked_stale(make_manager, pool, clock):
    mgr = make_manager()
    async with pool.acquire() as con:
        await con.execute(
            "insert into credit_ledger (created_at, endpoint, is_paid, is_mock, expected_cost, status) "
            "values ($1, 'odds', true, true, 1, 'pending')", clock() - timedelta(minutes=1))
    assert await mgr.recover_stale_pending() == 0


def _failing_client(exc):
    def handler(request):
        raise exc
    return OddsApiClient("https://api.the-odds-api.com", "", transport=httpx.MockTransport(handler), is_mock=True)


async def test_timeout_is_unknown_counted_and_not_retried(pool, clock):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    client = OddsApiClient("https://x", "", transport=httpx.MockTransport(handler), is_mock=True)
    mgr = CreditManager(pool, client, clock=clock)
    out = await epl(mgr)
    assert not out.allowed and out.status == "unknown"
    assert len(calls) == 1  # never retried
    (row,) = await ledger(pool)
    assert row["status"] == "unknown" and row["charged"] is None
    assert (await mgr.budget()).used == 1
    await client.aclose()


async def test_connect_error_is_not_sent_and_free(pool, clock):
    client = _failing_client(httpx.ConnectError("refused"))
    mgr = CreditManager(pool, client, clock=clock)
    out = await epl(mgr)
    assert out.status == "not_sent"
    (row,) = await ledger(pool)
    assert row["charged"] == 0 and (await mgr.budget()).used == 0
    await client.aclose()


async def test_out_of_credits_pauses_until_next_month(make_manager, pool, mock_api, clock):
    mock_api.used = 500
    mgr = make_manager(limit=1000, reserve=0, pause_pct=100)  # our own lines would still allow it
    out = await epl(mgr)
    assert not out.allowed and out.reason == "OUT_OF_USAGE_CREDITS" and out.status == "error"
    b = await mgr.budget()
    assert b.exhausted and b.paused
    assert not (await epl(mgr, kind="manual")).allowed
    clock.now = clock.now.replace(month=12, day=1, hour=1)  # new UTC month
    mock_api.used = 0
    b = await mgr.budget()
    assert not b.exhausted and b.used == 0


async def test_month_rollover_resets_counting(make_manager, pool, clock):
    mgr = make_manager()
    for _ in range(3):
        await epl(mgr)
    assert (await mgr.budget()).used == 3
    clock.now = clock.now.replace(month=12, day=1, hour=0, minute=5)
    assert (await mgr.budget()).used == 0


async def test_concurrent_calls_cannot_overspend(make_manager, pool, mock_api, clock):
    mock_api.used = 499
    mgr = make_manager(limit=500, reserve=0, pause_pct=100)  # exactly 1 credit left
    async with pool.acquire() as con:
        await con.execute(
            "insert into credit_ledger (created_at, finished_at, endpoint, is_paid, is_mock, expected_cost, charged, "
            "used_after, remaining_after, http_status, status) values ($1, $1, 'odds', true, true, 1, 1, 499, 1, 200, 'ok')",
            clock())
    # Force the race: pause between reading the budget and inserting the
    # pending row, so all five copies would read "1 left" without the lock.
    original = mgr._budget

    async def slow_budget(con):
        result = await original(con)
        await asyncio.sleep(0.2)
        return result

    mgr._budget = slow_budget
    sent_before = len(mock_api.requests)
    results = await asyncio.gather(*(epl(mgr) for _ in range(5)))
    # Count what actually went out, not what succeeded: without the lock the
    # API itself would reject the extra calls, which would hide the bug.
    assert len(mock_api.requests) - sent_before == 1
    assert sum(1 for r in await ledger(pool) if r["is_paid"] and r["kind"] == "planned") == 1
    assert sum(r.allowed for r in results) == 1
    assert mock_api.used == 500


async def test_restart_safe_new_manager_sees_same_spend(make_manager, client, pool, clock):
    first = make_manager()
    await epl(first)
    await epl(first)
    second = CreditManager(pool, client, clock=clock)  # e.g. after a redeploy
    assert (await second.budget()).used == 2


async def test_real_and_mock_ledgers_are_separate(make_manager, pool, clock):
    mgr = make_manager()
    await epl(mgr)
    async with pool.acquire() as con:
        await con.execute(
            "insert into credit_ledger (created_at, finished_at, endpoint, is_paid, is_mock, expected_cost, charged, "
            "used_after, remaining_after, http_status, status) values ($1, $1, 'odds', true, false, 1, 1, 200, 300, 200, 'ok')",
            clock())
    assert (await mgr.budget()).used == 1  # mock mode ignores the real row


async def test_soft_cap_refuses_extra_but_not_planned(make_manager):
    mgr = make_manager()
    b = await mgr.budget()
    cap = b.soft_cap_today
    for _ in range(cap):
        assert (await epl(mgr, kind="extra")).allowed
    assert not (await epl(mgr, kind="extra")).allowed
    assert (await epl(mgr, kind="planned")).allowed


async def test_missing_cost_header_counts_expected(pool, clock):
    def handler(request):
        return httpx.Response(200, json=[{"id": "x"}])  # no usage headers
    client = OddsApiClient("https://x", "", transport=httpx.MockTransport(handler), is_mock=True)
    mgr = CreditManager(pool, client, clock=clock)
    out = await epl(mgr)
    assert out.status == "unknown"
    assert (await mgr.budget()).used == 1
    await client.aclose()

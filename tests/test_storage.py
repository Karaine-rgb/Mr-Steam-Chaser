"""Storage guard (logic copied from the predecessor tool, tests adapted) and
instant round delete with background purge."""
from datetime import datetime, timedelta, timezone

import pytest

from app.round_delete import DELETING_LABEL, RoundDeleter
from app.storage_guard import StorageGuard, describe_usage


def test_describe_usage_levels_and_free_space():
    d = lambda used: describe_usage(used, 1000, 80, 90, 95)
    assert d(500)["level"] == "ok"
    assert d(800)["level"] == "warning"
    assert d(900)["level"] == "critical"
    assert d(950)["level"] == "paused"
    u = d(700)
    assert u["free_bytes"] == 300 and u["used_pct"] == 70.0
    assert u["pause_bytes"] == 950 and u["usable_bytes"] == 250
    assert d(1200)["free_bytes"] == 0


class Sizer:
    def __init__(self, size):
        self.size, self.calls = size, 0

    async def __call__(self):
        self.calls += 1
        if isinstance(self.size, Exception):
            raise self.size
        return self.size


class Tick:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(sizer, clock=None):
    return StorageGuard(1000, 80, 90, 95, size_fn=sizer, clock=clock or Tick())


async def test_pauses_at_threshold_and_resumes_only_below_the_margin():
    s, c = Sizer(960), Tick()
    g = make(s, c)
    assert await g.collection_blocked() is True
    for size, expected in ((940, True), (931, True), (929, False)):  # resume below 95 - 2 = 93 %
        s.size = size
        c.t += 61
        assert await g.collection_blocked() is expected, size
    s.size = 960
    c.t += 61
    assert await g.collection_blocked() is True


async def test_usage_is_cached_between_checks():
    s, c = Sizer(100), Tick()
    g = make(s, c)
    await g.usage()
    await g.usage()
    assert s.calls == 1
    c.t += 61
    await g.usage()
    await g.usage(force=True)
    assert s.calls == 3


async def test_guard_fails_open_when_size_cannot_be_read():
    assert await make(Sizer(RuntimeError("db down"))).collection_blocked() is False


# ---- round delete on a real database -------------------------------------

async def seed_round(pool, label, n=4, monitored=False, snapshots=3):
    now = datetime.now(timezone.utc)
    async with pool.acquire() as con:
        await con.execute("insert into round_settings (round_label, bookmakers) values ($1, '{pinnacle}')", label)
        for i in range(n):
            mid = await con.fetchval(
                "insert into matchups (event_id, sport_key, home_team, away_team, commence_time, round_label, is_monitored) "
                "values ($1, 'soccer_epl', 'H', 'A', $2, $3, $4) returning id",
                f"{label}-{i}".ljust(32, "x"), now + timedelta(days=1), label, monitored)
            for k in range(snapshots):
                await con.execute(
                    "insert into odds_snapshots (matchup_id, bookmaker, market, captured_at, home_price, draw_price, away_price) "
                    "values ($1, 'pinnacle', 'h2h', $2, 2, 3, 4)", mid, now - timedelta(hours=k))


async def counts(pool):
    async with pool.acquire() as con:
        return (await con.fetchval("select count(*) from matchups"),
                await con.fetchval("select count(*) from odds_snapshots"))


async def test_delete_is_instant_then_purged(pool):
    await seed_round(pool, "Old")
    await seed_round(pool, "Keep", n=2)
    d = RoundDeleter(pool)
    assert await d.hide_round("Old") == 4
    async with pool.acquire() as con:
        assert await con.fetchval("select count(*) from matchups where round_label = 'Old'") == 0
        assert await con.fetchval("select count(*) from round_settings where round_label = 'Old'") == 0
    assert await d.pending_count() == 4
    await d.start_purge()
    assert await d.pending_count() == 0
    assert await counts(pool) == (2, 6)  # the other round is untouched


async def test_delete_refused_while_monitored(pool):
    await seed_round(pool, "Live", monitored=True)
    d = RoundDeleter(pool)
    assert await d.hide_round("Live") == -1
    assert await counts(pool) == (4, 12)


async def test_interrupted_purge_resumes(pool):
    await seed_round(pool, "Old")
    d = RoundDeleter(pool)
    await d.hide_round("Old")
    # simulate a restart before the purge ran: a new deleter finds the marker
    assert await RoundDeleter(pool).purge_pending() == 4
    assert await counts(pool) == (0, 0)


async def test_unknown_round_is_zero(pool):
    assert await RoundDeleter(pool).hide_round("nope") == 0
    assert await RoundDeleter(pool).hide_round(DELETING_LABEL) == 0

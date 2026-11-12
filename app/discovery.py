"""Discovery: cached FREE listings, cost estimates, adding matches to a round.

Nothing in this module spends credits. /sports and /events are free
(verified live: x-requests-last 0). They are still cached so repeated clicks
do not hit the API at all: the sports list for 24 h, each league's events
for 10 min. Adding matches to a round only writes to the database; the
poller (Phase 4) decides when to pay for odds, through the credit gate.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from app.catalog import bookmaker_keys, paid_only_keys, soccer_leagues
from app.credits import CreditManager
from app.parse import parse_time
from app.planner import PRESETS, TrackedMatch, month_bounds, selection_estimate

SPORTS_TTL_S = 24 * 3600
EVENTS_TTL_S = 10 * 60
ALLOWED_MARKETS = ("h2h", "spreads", "totals")
EVENT_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_LABEL = 120


class DiscoveryError(ValueError):
    pass


@dataclass
class _Entry:
    value: object
    at: float


@dataclass
class DiscoveryCache:
    clock: callable = time.monotonic
    sports: _Entry | None = None
    events: dict = field(default_factory=dict)
    locks: dict = field(default_factory=dict)

    def lock(self, key: str) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())


class Discovery:
    def __init__(self, credits: CreditManager, cache: DiscoveryCache | None = None) -> None:
        self.credits = credits
        self.cache = cache or DiscoveryCache()

    # ---- free listings (cached) --------------------------------------
    async def sports(self, refresh: bool = False) -> tuple[list[dict], float]:
        c = self.cache
        async with c.lock("sports"):
            if refresh or c.sports is None or c.clock() - c.sports.at > SPORTS_TTL_S:
                result = await self.credits.free_sports(kind="discovery", all_sports=True)
                if not result.ok:
                    raise DiscoveryError(f"sports list failed: {result.error_code or result.status}")
                c.sports = _Entry(result.data, c.clock())
            return c.sports.value, c.clock() - c.sports.at

    async def leagues(self, include_inactive: bool = False, refresh: bool = False) -> dict:
        sports, age = await self.sports(refresh)
        return {"leagues": soccer_leagues(sports, include_inactive), "age_s": round(age)}

    async def events(self, sport: str, refresh: bool = False) -> tuple[list[dict], float]:
        valid = {l["key"] for l in soccer_leagues((await self.sports())[0], include_inactive=True)}
        if sport not in valid:
            raise DiscoveryError(f"unknown soccer league {sport!r}")
        c = self.cache
        async with c.lock(f"events:{sport}"):
            entry = c.events.get(sport)
            if refresh or entry is None or c.clock() - entry.at > EVENTS_TTL_S:
                result = await self.credits.free_events(sport, kind="discovery")
                if not result.ok:
                    raise DiscoveryError(f"events for {sport} failed: {result.error_code or result.status}")
                entry = c.events[sport] = _Entry(result.data or [], c.clock())
            return entry.value, c.clock() - entry.at

    async def events_for(self, sports: list[str], refresh: bool = False) -> dict:
        titles = {l["key"]: l["title"] for l in soccer_leagues((await self.sports())[0], include_inactive=True)}
        now = self.credits.clock()
        tracked = await self._tracked_index()
        out, ages = [], []
        for sport in dict.fromkeys(sports):
            events, age = await self.events(sport, refresh)
            ages.append(age)
            for e in events:
                kickoff = parse_time(e.get("commence_time"))
                if kickoff is None or kickoff <= now:
                    continue  # started or live: cannot be tracked pre-match
                out.append({
                    "event_id": e["id"], "sport_key": sport, "league_title": titles.get(sport, sport),
                    "home_team": e.get("home_team"), "away_team": e.get("away_team"),
                    "commence_time": kickoff.isoformat(), "tracked_in": tracked.get(e["id"]),
                })
        out.sort(key=lambda x: (x["commence_time"], x["league_title"], x["home_team"] or ""))
        return {"events": out, "max_age_s": round(max(ages)) if ages else 0}

    # ---- database helpers --------------------------------------------
    async def _tracked_index(self) -> dict[str, str]:
        async with self.credits.pool.acquire() as con:
            rows = await con.fetch("select event_id, coalesce(round_label, '(unlabelled)') as r from matchups "
                                   "where round_label is distinct from '__deleting__'")
        return {r["event_id"]: r["r"] for r in rows}

    async def tracked_matches(self) -> list[TrackedMatch]:
        async with self.credits.pool.acquire() as con:
            rows = await con.fetch(
                "select m.event_id, m.sport_key, m.commence_time, m.added_at, "
                "coalesce(s.schedule_preset, 'balanced') as preset, "
                "coalesce(s.markets, '{h2h}') as markets, coalesce(s.bookmakers, '{}') as bookmakers "
                "from matchups m left join round_settings s on s.round_label = m.round_label "
                "where m.is_monitored")
        return [TrackedMatch(r["event_id"], r["sport_key"], r["commence_time"], r["added_at"], r["preset"],
                             tuple(r["markets"]), tuple(r["bookmakers"])) for r in rows]

    async def league_call_history(self) -> dict[str, list[datetime]]:
        start, _ = month_bounds(self.credits.clock())
        async with self.credits.pool.acquire() as con:
            rows = await con.fetch(
                "select sport_key, coalesce(finished_at, created_at) as t from credit_ledger "
                "where is_paid and endpoint = 'odds' and is_mock = $1 and status in ('ok', 'empty') "
                "and created_at >= $2::timestamptz - interval '3 days'", self.credits.is_mock, start)
        out: dict[str, list[datetime]] = {}
        for r in rows:
            out.setdefault(r["sport_key"], []).append(r["t"])
        return out

    # ---- validation ----------------------------------------------------
    @staticmethod
    def check_selection(bookmakers: list[str], markets: list[str], preset: str) -> tuple[list[str], list[str]]:
        books = list(dict.fromkeys(b.strip() for b in bookmakers if b.strip()))
        if not books:
            raise DiscoveryError("pick at least one bookmaker")
        unknown = [b for b in books if b not in bookmaker_keys()]
        if unknown:
            raise DiscoveryError(f"unknown bookmaker(s): {', '.join(unknown)}")
        paid = [b for b in books if b in paid_only_keys()]
        if paid:
            raise DiscoveryError(f"not available on the free plan: {', '.join(paid)}")
        mk = list(dict.fromkeys(markets or ["h2h"]))
        if "h2h" not in mk or any(m not in ALLOWED_MARKETS for m in mk):
            raise DiscoveryError("markets must include h2h and may add spreads and/or totals")
        if preset not in PRESETS:
            raise DiscoveryError(f"unknown schedule {preset!r}")
        return books, mk

    async def _resolve(self, picks: list[dict]) -> list[dict]:
        """Look every picked match up in OUR cached API data (never trust the
        browser's copy of team names or kickoff times)."""
        by_sport: dict[str, set[str]] = {}
        for p in picks:
            eid, sport = str(p.get("event_id", "")), str(p.get("sport_key", ""))
            if not EVENT_ID.match(eid):
                raise DiscoveryError(f"bad event id {eid!r}")
            by_sport.setdefault(sport, set()).add(eid)
        now = self.credits.clock()
        found = []
        for sport, ids in by_sport.items():
            events, _ = await self.events(sport)
            index = {e["id"]: e for e in events}
            missing = ids - index.keys()
            if missing:
                raise DiscoveryError(f"{len(missing)} match(es) no longer listed for {sport}; reload the list")
            for eid in ids:
                e = index[eid]
                kickoff = parse_time(e["commence_time"])
                if kickoff is None or kickoff <= now:
                    raise DiscoveryError(f"{e['home_team']} v {e['away_team']} has already kicked off")
                found.append({"event_id": eid, "sport_key": sport, "home_team": e["home_team"],
                              "away_team": e["away_team"], "commence_time": kickoff})
        return found

    # ---- estimate and add ----------------------------------------------
    async def estimate(self, picks: list[dict], bookmakers: list[str], markets: list[str], preset: str) -> dict:
        books, mk = self.check_selection(bookmakers, markets, preset)
        now = self.credits.clock()
        existing = await self.tracked_matches()
        tracked_ids = {m.event_id for m in existing}
        resolved = await self._resolve(picks) if picks else []
        new = [TrackedMatch(m["event_id"], m["sport_key"], m["commence_time"], now, preset, tuple(mk), tuple(books))
               for m in resolved if m["event_id"] not in tracked_ids]
        budget = await self.credits.budget()
        est = selection_estimate(existing, new, await self.league_call_history(), now, budget.auto_left)
        est.update({
            "matches": len(new), "leagues": len({m.sport_key for m in new}),
            "already_tracked": len(resolved) - len(new),
            "cost_per_league_refresh": len(mk) * -(-len(books) // 10),
            "bookmakers": len(books), "markets": mk, "preset": preset,
            "budget": budget.as_dict(),
        })
        return est

    async def add_to_round(self, round_label: str, picks: list[dict], bookmakers: list[str],
                           markets: list[str], preset: str) -> dict:
        label = (round_label or "").strip()
        if not label or len(label) > MAX_LABEL or label.startswith("__"):
            raise DiscoveryError(f"round label must be 1-{MAX_LABEL} characters")
        books, mk = self.check_selection(bookmakers, markets, preset)
        if not picks:
            raise DiscoveryError("no matches selected")
        resolved = await self._resolve(picks)
        titles = {l["key"]: l["title"] for l in soccer_leagues((await self.sports())[0], include_inactive=True)}
        now = self.credits.clock()
        async with self.credits.pool.acquire() as con:
            async with con.transaction():
                taken = {r["event_id"]: r["round_label"] for r in await con.fetch(
                    "select event_id, round_label from matchups where event_id = any($1::text[])",
                    [m["event_id"] for m in resolved])}
                clash = {e: r for e, r in taken.items() if r != label}
                if clash:
                    raise DiscoveryError(f"{len(clash)} match(es) are already in another round "
                                         f"({', '.join(sorted(set(clash.values())))})")
                await con.execute(
                    "insert into round_settings (round_label, bookmakers, markets, schedule_preset) "
                    "values ($1, $2, $3, $4) on conflict (round_label) do update set "
                    "bookmakers = excluded.bookmakers, markets = excluded.markets, "
                    "schedule_preset = excluded.schedule_preset", label, books, mk, preset)
                added = 0
                for m in resolved:
                    if m["event_id"] in taken:
                        continue
                    await con.execute(
                        "insert into matchups (event_id, sport_key, league_title, home_team, away_team, "
                        "commence_time, round_label, is_monitored, added_at) values ($1,$2,$3,$4,$5,$6,$7,true,$8)",
                        m["event_id"], m["sport_key"], titles.get(m["sport_key"]), m["home_team"],
                        m["away_team"], m["commence_time"], label, now)
                    added += 1
        return {"round_label": label, "added": added, "already_in_round": len(resolved) - added,
                "credits_spent": 0}

    async def rounds(self) -> list[dict]:
        async with self.credits.pool.acquire() as con:
            rows = await con.fetch(
                "select m.round_label, count(*) as matches, count(*) filter (where m.is_monitored) as monitored, "
                "min(m.commence_time) as first_kickoff, max(m.commence_time) as last_kickoff, "
                "s.bookmakers, s.markets, s.schedule_preset "
                "from matchups m left join round_settings s on s.round_label = m.round_label "
                "where m.round_label is not null and m.round_label <> '__deleting__' "
                "group by m.round_label, s.bookmakers, s.markets, s.schedule_preset "
                "order by min(m.commence_time) desc")
        return [{**dict(r), "first_kickoff": r["first_kickoff"].isoformat(),
                 "last_kickoff": r["last_kickoff"].isoformat()} for r in rows]

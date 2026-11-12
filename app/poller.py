"""Background poller, running inside the web process.

Every tick (default 30 s; cheap database work only):
  1. Take or renew the poller LEASE, a row in app_settings that expires
     after 90 s. Only the holder polls, so two app copies (for example during
     a Railway redeploy) do not both pay for the same refresh. A lease row
     works through any connection pooler; a session advisory lock would not
     survive a transaction-mode pooler. Overspending is prevented separately
     by the credit gate (app/credits.py), whatever happens here.
  2. Mark stale pending paid calls as 'unknown'.
  3. Stop monitoring every match whose kickoff has passed (never poll after
     kickoff).
  4. If the database is near its size limit, or credits are paused: stop.
  5. Build the month's plan (app/planner.py) and make AT MOST ONE paid call:
     the league whose next planned snapshot is due, nearest kickoff first.
     A league whose last call failed waits (15 min, doubling, up to 6 h)
     before it is tried again, so a broken request cannot drain credits.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta

from app.credits import CreditManager, utcnow
from app.discovery import Discovery
from app.ingest import ingest_odds
from app.planner import build_plan, estimate_cost, month_bounds, next_due_call

logger = logging.getLogger("poller")

LEASE_KEY = "poller_lease"
LEASE_SECONDS = 90
BACKOFF_BASE = timedelta(minutes=15)
BACKOFF_MAX = timedelta(hours=6)
FAILED = ("error", "unknown", "not_sent")


class Poller:
    def __init__(self, pool, credits: CreditManager, discovery: Discovery, guard=None,
                 clock=utcnow, tick_seconds: float = 30.0) -> None:
        self.pool = pool
        self.credits = credits
        self.discovery = discovery
        self.guard = guard
        self.clock = clock
        self.tick_seconds = tick_seconds
        self.instance = uuid.uuid4().hex[:12]
        self.leader = False
        self.last_tick: dict = {}
        self.last_plan = None
        self.version = 0  # bumps whenever stored data or monitoring changes
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._call_lock = asyncio.Lock()

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
        await self._release_lease()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.last_tick = await self.tick()
            except Exception:
                logger.exception("Poller tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
            except asyncio.TimeoutError:
                pass

    # ---- lease ------------------------------------------------------------
    async def _lease(self, now: datetime) -> bool:
        value = f"{self.instance}|{(now + timedelta(seconds=LEASE_SECONDS)).isoformat()}"
        async with self.pool.acquire() as con:
            got = await con.fetchval(
                "insert into app_settings (key, value) values ($1, $2) on conflict (key) do update "
                "set value = excluded.value where split_part(app_settings.value, '|', 1) = $3 "
                "or split_part(app_settings.value, '|', 2)::timestamptz < $4 returning value",
                LEASE_KEY, value, self.instance, now)
        self.leader = got is not None
        return self.leader

    async def _release_lease(self) -> None:
        try:
            async with self.pool.acquire() as con:
                await con.execute("delete from app_settings where key = $1 and split_part(value, '|', 1) = $2",
                                  LEASE_KEY, self.instance)
        except Exception:
            logger.warning("Could not release the poller lease", exc_info=True)
        self.leader = False

    # ---- one tick ---------------------------------------------------------
    async def tick(self) -> dict:
        now = self.clock()
        if not await self._lease(now):
            return {"leader": False}
        await self.credits.recover_stale_pending()
        expired = await self._expire_kickoffs(now)
        if self.guard is not None and await self.guard.collection_blocked():
            return {"leader": True, "expired": expired, "skipped": "storage_paused"}
        budget = await self.credits.budget()
        _, month_end = month_bounds(now)
        matches = await self.discovery.tracked_matches()
        history = await self.discovery.league_call_history()
        plan = build_plan(matches, history, now, month_end, budget.auto_left)
        self.last_plan = plan
        if budget.paused:
            return {"leader": True, "expired": expired, "skipped": "credits_paused"}
        waiting = await self._backoff(now)
        plan_now = type(plan)([c for c in plan.calls if c.sport_key not in waiting], plan.dropped,
                              plan.cost_this_month, plan.available, plan.month_end, plan.dropped_calls)
        last = {lg: max(ts) for lg, ts in history.items() if ts}
        call = next_due_call(plan_now, now, last)
        if call is None:
            return {"leader": True, "expired": expired, "called": None, "waiting": sorted(waiting)}
        labels = ",".join(sorted({p.label for p in call.points}))
        result = await self.poll_league(call.sport_key, "planned", f"planned {labels}")
        # Rebuild so the plan shown in the UI no longer lists what was just done.
        budget = await self.credits.budget()
        self.last_plan = build_plan(await self.discovery.tracked_matches(),
                                    await self.discovery.league_call_history(), self.clock(), month_end,
                                    budget.auto_left)
        return {"leader": True, "expired": expired, "called": call.sport_key, "result": result}

    async def _expire_kickoffs(self, now: datetime) -> int:
        async with self.pool.acquire() as con:
            rows = await con.fetch("update matchups set is_monitored = false "
                                   "where is_monitored and commence_time <= $1 returning id", now)
        if rows:
            self.version += 1
            logger.info("Stopped monitoring %d match(es) at kickoff", len(rows))
        return len(rows)

    async def _backoff(self, now: datetime) -> set[str]:
        """Leagues whose recent paid calls failed and must wait before a retry."""
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "select sport_key, status, coalesce(finished_at, created_at) as t from credit_ledger "
                "where is_paid and endpoint = 'odds' and is_mock = $1 and created_at > $2 "
                "order by sport_key, created_at desc, id desc",
                self.credits.is_mock, now - BACKOFF_MAX * 2)
        streak: dict[str, tuple[int, datetime]] = {}
        done: set[str] = set()
        for r in rows:
            lg = r["sport_key"]
            if lg in done:
                continue
            if r["status"] in FAILED:
                n, t = streak.get(lg, (0, r["t"]))
                streak[lg] = (n + 1, t)
            else:
                done.add(lg)
        waiting = set()
        for lg, (n, last_fail) in streak.items():
            wait = min(BACKOFF_BASE * (2 ** (n - 1)), BACKOFF_MAX)
            if now < last_fail + wait:
                waiting.add(lg)
        return waiting

    # ---- the paid part ------------------------------------------------------
    async def league_request(self, sport: str, now: datetime) -> dict | None:
        """What a refresh of this league would ask for: tracked matches, the
        union of their rounds' bookmakers and markets, and the cost."""
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "select m.event_id, s.bookmakers, s.markets from matchups m "
                "join round_settings s on s.round_label = m.round_label "
                "where m.is_monitored and m.sport_key = $1 and m.commence_time > $2", sport, now)
        if not rows:
            return None
        books = sorted({b for r in rows for b in r["bookmakers"]})
        markets = sorted({mk for r in rows for mk in r["markets"]}, key=lambda m: (m != "h2h", m))
        return {"sport": sport, "event_ids": sorted({r["event_id"] for r in rows}), "bookmakers": books,
                "markets": markets, "cost": estimate_cost(markets, books)}

    async def poll_league(self, sport: str, kind: str, reason: str) -> dict:
        async with self._call_lock:
            now = self.clock()
            req = await self.league_request(sport, now)
            if req is None:
                return {"sport": sport, "status": "nothing_to_poll"}
            outcome = await self.credits.paid_odds(
                sport, req["markets"], req["bookmakers"], kind, reason, event_ids=req["event_ids"],
                commence_from=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            out = {"sport": sport, "allowed": outcome.allowed, "status": outcome.status,
                   "reason": outcome.reason, "ledger_id": outcome.ledger_id}
            if outcome.result is not None and outcome.result.ok:
                # Re-read the clock: kickoff may have passed while the request was in flight.
                stored = await ingest_odds(self.pool, outcome.events, self.clock(), outcome.ledger_id)
                out["ingest"] = stored
                self.version += 1
            return out

    async def round_refresh_quote(self, round_label: str) -> dict:
        now = self.clock()
        async with self.pool.acquire() as con:
            leagues = [r["sport_key"] for r in await con.fetch(
                "select distinct sport_key from matchups where round_label = $1 and is_monitored "
                "and commence_time > $2 order by 1", round_label, now)]
        reqs = [r for r in [await self.league_request(lg, now) for lg in leagues] if r]
        return {"round_label": round_label, "leagues": [{"sport": r["sport"], "cost": r["cost"],
                                                         "matches": len(r["event_ids"])} for r in reqs],
                "cost": sum(r["cost"] for r in reqs)}

    async def refresh_round(self, round_label: str, expected_cost: int) -> dict:
        """Manual 'refresh now'. The caller must send the cost it showed the
        user; if the real cost differs, nothing is spent."""
        quote = await self.round_refresh_quote(round_label)
        if quote["cost"] != expected_cost:
            return {"ok": False, "reason": "cost_changed", "quote": quote}
        results = []
        for lg in quote["leagues"]:
            results.append(await self.poll_league(lg["sport"], "manual", f"manual refresh of {round_label}"))
        return {"ok": True, "results": results, "quote": quote}

    def plan_for(self, event_id: str) -> list[dict]:
        if self.last_plan is None:
            return []
        out = []
        for c in self.last_plan.calls:
            for p in c.points:
                if p.event_id == event_id:
                    out.append({"label": p.label, "at": c.time.isoformat(),
                                "this_month": c.time < self.last_plan.month_end})
        dropped = [{"label": p.label, "at": p.due.isoformat(), "dropped": True}
                   for p in self.last_plan.dropped if p.event_id == event_id]
        return sorted(out + dropped, key=lambda x: x["at"])

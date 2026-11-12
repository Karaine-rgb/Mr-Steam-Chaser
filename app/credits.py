"""Credit budget manager: the ONE gate for every paid API call.

docs/plan.md section 2. In short:
  1. check the budget, inside a Postgres advisory lock,
  2. write a 'pending' ledger row in that same transaction (so a second app
     copy sees the spend before the request even goes out),
  3. send the request exactly once (no retry),
  4. record the real cost from the response headers.
A request that dies without a response stays counted at its expected cost
('unknown') until a later response's x-requests-used shows the truth.

How "used this month" is worked out (UTC month, an assumption):
  - The API's own x-requests-used is the truth. The ledger row this month
    whose response arrived last and carries it is the "sync point".
  - used = sync value + expected cost of pending/unknown paid rows created
    after (sync time - 2 min). Without any sync this month, used = sum of
    charged credits + expected cost of pending/unknown rows.
  - Mock calls are counted only in mock mode, real calls only in real mode.

Lines (with the defaults 500 / 50 / 90 %, both lines are 450):
  pause line  = floor(limit * pause_pct / 100): every paid call stops here
  auto line   = min(limit - reserve, pause line): automatic polling stops here
Manual refreshes may go up to the pause line, never past it.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import asyncpg

from app.odds_api import ApiResult, OddsApiClient, OddsApiNetworkError
from app.planner import estimate_cost, month_bounds

logger = logging.getLogger("credits")

LEDGER_LOCK_KEY = 7_430_001  # arbitrary constant for pg_advisory_xact_lock
SYNC_MARGIN = timedelta(minutes=2)
AUTO_KINDS = ("planned", "extra")
UNCERTAIN = ("pending", "unknown")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Budget:
    limit: int
    reserve: int
    pause_pct: float
    used: int
    spent_today: int
    now: datetime
    api_used: int | None = None
    api_remaining: int | None = None
    api_synced_at: datetime | None = None
    uncertain: int = 0
    exhausted: bool = False

    @property
    def pause_line(self) -> int:
        return math.floor(self.limit * self.pause_pct / 100.0)

    @property
    def auto_line(self) -> int:
        return min(self.limit - self.reserve, self.pause_line)

    @property
    def paused(self) -> bool:
        return self.exhausted or self.used >= self.pause_line

    @property
    def auto_left(self) -> int:
        return max(0, self.auto_line - self.used)

    @property
    def manual_left(self) -> int:
        return 0 if self.exhausted else max(0, self.pause_line - self.used)

    @property
    def days_left(self) -> float:
        """Days left in the UTC month, counting today as a full day."""
        _, end = month_bounds(self.now)
        today = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1.0, (end - today).total_seconds() / 86400)

    @property
    def soft_cap_today(self) -> int:
        """Today's share of what was left at the start of today."""
        left_at_day_start = max(0, self.auto_line - (self.used - self.spent_today))
        return math.floor(left_at_day_start / self.days_left)

    @property
    def projected_month_end(self) -> int:
        start, end = month_bounds(self.now)
        elapsed = max(1 / 24, (self.now - start).total_seconds() / 86400)
        total = (end - start).total_seconds() / 86400
        return round(self.used / elapsed * total)

    @property
    def api_quota_mismatch(self) -> bool:
        """True when the API's used+remaining differs from MONTHLY_CREDIT_LIMIT."""
        if self.api_used is None or self.api_remaining is None:
            return False
        return self.api_used + self.api_remaining != self.limit

    def as_dict(self) -> dict:
        return {
            "limit": self.limit, "reserve": self.reserve, "pause_pct": self.pause_pct,
            "used": self.used, "uncertain": self.uncertain,
            "pause_line": self.pause_line, "auto_line": self.auto_line,
            "auto_left": self.auto_left, "manual_left": self.manual_left,
            "paused": self.paused, "exhausted": self.exhausted,
            "spent_today": self.spent_today, "soft_cap_today": self.soft_cap_today,
            "days_left": round(self.days_left, 2),
            "projected_month_end": self.projected_month_end,
            "api_used": self.api_used, "api_remaining": self.api_remaining,
            "api_synced_at": self.api_synced_at.isoformat() if self.api_synced_at else None,
            "api_quota_mismatch": self.api_quota_mismatch,
        }


def decide(b: Budget, cost: int, kind: str) -> tuple[bool, str]:
    """May a paid call of `cost` credits of this `kind` go out now?"""
    if kind not in ("planned", "extra", "manual"):
        return False, f"unknown call kind {kind!r}"
    if cost <= 0:
        return False, "a paid call must have a positive expected cost"
    if b.exhausted:
        return False, "the API reported OUT_OF_USAGE_CREDITS this month"
    if b.paused:
        return False, f"paused: {b.used} of {b.limit} credits used (pause line {b.pause_line})"
    if kind == "manual":
        if b.used + cost > b.pause_line:
            return False, f"would pass the pause line ({b.used} + {cost} > {b.pause_line})"
        return True, "ok"
    if b.used + cost > b.auto_line:
        return False, f"would pass the automatic line ({b.used} + {cost} > {b.auto_line}); the reserve is never spent automatically"
    if kind == "extra" and b.spent_today + cost > b.soft_cap_today:
        return False, f"today's soft cap reached ({b.spent_today} + {cost} > {b.soft_cap_today})"
    return True, "ok"


@dataclass
class PaidOutcome:
    allowed: bool
    reason: str
    ledger_id: int | None = None
    result: ApiResult | None = None
    status: str | None = None
    budget: Budget | None = None
    events: list = field(default_factory=list)


class CreditManager:
    def __init__(
        self,
        pool: asyncpg.Pool,
        client: OddsApiClient,
        limit: int = 500,
        reserve: int = 50,
        pause_pct: float = 90.0,
        pending_stale_minutes: float = 5.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.pool = pool
        self.client = client
        self.limit = limit
        self.reserve = reserve
        self.pause_pct = pause_pct
        self.pending_stale = timedelta(minutes=pending_stale_minutes)
        self.clock = clock
        self.is_mock = client.is_mock

    # ---- reading the budget -----------------------------------------
    async def budget(self, con: asyncpg.Connection | None = None) -> Budget:
        if con is None:
            async with self.pool.acquire() as c:
                return await self._budget(c)
        return await self._budget(con)

    async def _budget(self, con: asyncpg.Connection) -> Budget:
        now = self.clock()
        start, _ = month_bounds(now)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        mock = self.is_mock

        sync = await con.fetchrow(
            # newest RESPONSE wins: order by when the call finished, not started
            "select finished_at as created_at, used_after, remaining_after from credit_ledger "
            "where is_mock = $1 and created_at >= $2 and used_after is not null and finished_at is not null "
            "order by finished_at desc, id desc limit 1",
            mock, start,
        )
        totals = await con.fetchrow(
            "select coalesce(sum(charged) filter (where charged is not null and status not in ('pending','unknown')), 0) as known, "
            "coalesce(sum(expected_cost) filter (where status in ('pending','unknown')), 0) as uncertain_all, "
            "coalesce(sum(expected_cost) filter (where status in ('pending','unknown') and created_at > $3), 0) as uncertain_after, "
            "coalesce(sum(coalesce(charged, expected_cost)) filter (where created_at >= $4 and status <> 'not_sent'), 0) as today "
            "from credit_ledger where is_mock = $1 and is_paid and created_at >= $2",
            mock, start, (sync["created_at"] - SYNC_MARGIN) if sync else start, today,
        )
        exhausted_row = await con.fetchrow(
            "select created_at from credit_ledger where is_mock = $1 and created_at >= $2 "
            "and error_code = 'OUT_OF_USAGE_CREDITS' order by created_at desc limit 1",
            mock, start,
        )
        exhausted = False
        if exhausted_row:
            later_ok = await con.fetchval(
                "select count(*) from credit_ledger where is_mock = $1 and created_at > $2 "
                "and remaining_after > 0 and http_status between 200 and 299",
                mock, exhausted_row["created_at"],
            )
            exhausted = not later_ok

        if sync:
            uncertain = int(totals["uncertain_after"])
            used = int(sync["used_after"]) + uncertain
        else:
            uncertain = int(totals["uncertain_all"])
            used = int(totals["known"]) + uncertain
        return Budget(
            limit=self.limit, reserve=self.reserve, pause_pct=self.pause_pct,
            used=used, spent_today=int(totals["today"]), now=now,
            api_used=sync["used_after"] if sync else None,
            api_remaining=sync["remaining_after"] if sync else None,
            api_synced_at=sync["created_at"] if sync else None,
            uncertain=uncertain, exhausted=exhausted,
        )

    async def recent_calls(self, limit: int = 20, paid_only: bool = True) -> list[dict]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "select id, created_at, endpoint, kind, sport_key, markets, bookmakers, expected_cost, "
                "charged, used_after, remaining_after, http_status, events_returned, error_code, status, reason "
                "from credit_ledger where is_mock = $1 and ($2 = false or is_paid) "
                "order by created_at desc, id desc limit $3",
                self.is_mock, paid_only, limit,
            )
        return [dict(r) for r in rows]

    # ---- ledger writes ----------------------------------------------
    async def _locked_execute(self, sql: str, *args) -> str:
        """Every ledger write takes the same advisory lock as the budget check
        in paid_odds. Without it, a call finishing between the check's two
        queries made the check undercount by that call (a real race that a
        test caught)."""
        async with self.pool.acquire() as con:
            async with con.transaction():
                await con.execute("select pg_advisory_xact_lock($1)", LEDGER_LOCK_KEY)
                return await con.execute(sql, *args)

    # ---- housekeeping -----------------------------------------------
    async def recover_stale_pending(self) -> int:
        """Pending rows older than the stale limit come from a crash or a hung
        request: mark them 'unknown' (still counted at expected cost)."""
        cutoff = self.clock() - self.pending_stale
        result = await self._locked_execute(
                "update credit_ledger set status = 'unknown', finished_at = $2, "
                "reason = coalesce(reason, '') || ' [no response recorded; counted at expected cost]' "
                "where status = 'pending' and created_at < $1",
                cutoff, self.clock(),
        )
        return int(result.split()[-1])

    async def _log_free(self, endpoint: str, result: ApiResult | None, kind: str,
                        sport_key: str | None, error: str | None = None) -> None:
        if result is not None and result.usage.last:
            logger.warning("Free endpoint %s reported a cost of %s credits", endpoint, result.usage.last)
        await self._locked_execute(
                "insert into credit_ledger (created_at, finished_at, endpoint, is_paid, is_mock, kind, sport_key, "
                "expected_cost, charged, used_after, remaining_after, http_status, events_returned, error_code, status, reason) "
                "values ($1, $1, $2, false, $3, $4, $5, 0, $6, $7, $8, $9, $10, $11, $12, $13)",
                self.clock(), endpoint, self.is_mock, kind, sport_key,
                result.usage.last if result else None,
                result.usage.used if result else None,
                result.usage.remaining if result else None,
                result.status if result else None,
                result.event_count if result else None,
                result.error_code if result else None,
                ("ok" if result.ok else "error") if result else "not_sent",
                error,
        )

    # ---- free calls (logged, never gated) ----------------------------
    async def free_sports(self, kind: str = "discovery", all_sports: bool = True) -> ApiResult:
        try:
            result = await self.client.get_sports(all_sports)
        except OddsApiNetworkError as exc:
            await self._log_free("sports", None, kind, None, str(exc))
            raise
        await self._log_free("sports", result, kind, None)
        return result

    async def free_events(self, sport: str, kind: str = "discovery",
                          commence_from: str | None = None, commence_to: str | None = None) -> ApiResult:
        try:
            result = await self.client.get_events(sport, commence_from, commence_to)
        except OddsApiNetworkError as exc:
            await self._log_free("events", None, kind, sport, str(exc))
            raise
        await self._log_free("events", result, kind, sport)
        return result

    async def sync(self) -> Budget:
        """Re-sync with the API's real counter using the FREE /sports call."""
        await self.free_sports(kind="sync")
        return await self.budget()

    # ---- the paid gate ----------------------------------------------
    async def paid_odds(
        self,
        sport: str,
        markets: list[str],
        bookmakers: list[str],
        kind: str,
        reason: str,
        event_ids: list[str] | None = None,
        commence_from: str | None = None,
        commence_to: str | None = None,
    ) -> PaidOutcome:
        expected = estimate_cost(markets, bookmakers)
        async with self.pool.acquire() as con:
            async with con.transaction():
                await con.execute("select pg_advisory_xact_lock($1)", LEDGER_LOCK_KEY)
                b = await self._budget(con)
                allowed, why = decide(b, expected, kind)
                if not allowed:
                    logger.info("Paid call refused (%s, %s): %s", sport, kind, why)
                    return PaidOutcome(False, why, budget=b)
                ledger_id = await con.fetchval(
                    "insert into credit_ledger (created_at, endpoint, is_paid, is_mock, kind, sport_key, markets, "
                    "bookmakers, events_requested, expected_cost, status, reason) "
                    "values ($1, 'odds', true, $2, $3, $4, $5, $6, $7, $8, 'pending', $9) returning id",
                    self.clock(), self.is_mock, kind, sport, ",".join(markets), ",".join(bookmakers),
                    len(event_ids) if event_ids else None, expected, reason,
                )

        try:
            result = await self.client._get_odds_unchecked(
                sport, markets, bookmakers, event_ids, commence_from, commence_to
            )
        except OddsApiNetworkError as exc:
            status = "unknown" if exc.maybe_charged else "not_sent"
            await self._finish(ledger_id, None, status, str(exc), charged=None if exc.maybe_charged else 0)
            return PaidOutcome(False, f"request failed: {exc}", ledger_id, None, status)
        except BaseException as exc:
            # Includes RealApiForbidden and cancellation: nothing was confirmed.
            sent = not type(exc).__name__ == "RealApiForbidden"
            await self._finish(ledger_id, None, "unknown" if sent else "not_sent", type(exc).__name__,
                               charged=None if sent else 0)
            raise

        if result.usage.last is None:
            status = "unknown"  # no cost header: keep counting the expected cost
        elif not result.ok:
            status = "error"
        elif not result.data:
            status = "empty"
        else:
            status = "ok"
        await self._finish(ledger_id, result, status, None, charged=result.usage.last)
        return PaidOutcome(result.ok, "ok" if result.ok else (result.error_code or f"HTTP {result.status}"),
                           ledger_id, result, status, events=result.data if result.ok else [])

    async def _finish(self, ledger_id: int, result: ApiResult | None, status: str,
                      error: str | None, charged: int | None) -> None:
        await self._locked_execute(
                "update credit_ledger set finished_at = $2, status = $3, charged = $4, used_after = $5, "
                "remaining_after = $6, http_status = $7, events_returned = $8, error_code = $9, "
                "reason = case when $10::text is null then reason else coalesce(reason, '') || ' [' || $10 || ']' end "
                "where id = $1",
                ledger_id, self.clock(), status, charged,
                result.usage.used if result else None,
                result.usage.remaining if result else None,
                result.status if result else None,
                result.event_count if result else None,
                result.error_code if result else None,
                error,
        )

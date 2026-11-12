"""FastAPI app: credits, discovery, dashboard, storage. The poller runs in-process."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import dashboard, db
from app.config import Settings, load_settings
from app.catalog import bookmakers as bookmaker_catalog
from app.credits import CreditManager
from app.discovery import Discovery, DiscoveryError
from app.odds_api import OddsApiClient, OddsApiNetworkError, build_client
from app.planner import estimate_cost
from app.poller import Poller
from app.round_delete import RoundDeleter
from app.storage_guard import guard_for_pool

logger = logging.getLogger("api")
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


class CostQuery(BaseModel):
    markets: list[str] = Field(default_factory=lambda: ["h2h"])
    bookmakers: list[str] = Field(default_factory=list)


class Pick(BaseModel):
    event_id: str
    sport_key: str


class Selection(BaseModel):
    matches: list[Pick] = Field(default_factory=list)
    bookmakers: list[str] = Field(default_factory=list)
    markets: list[str] = Field(default_factory=lambda: ["h2h"])
    preset: str = "balanced"


class AddToRound(Selection):
    round_label: str


class MatchResult(BaseModel):
    home_score: int | None = None
    away_score: int | None = None


class RefreshRequest(BaseModel):
    round_label: str
    expected_cost: int


class AppState:
    settings: Settings
    client: OddsApiClient
    credits: CreditManager
    discovery: Discovery
    pool: object
    guard: object
    deleter: RoundDeleter
    poller: Poller


def create_app(settings: Settings | None = None, client: OddsApiClient | None = None, pool=None) -> FastAPI:
    settings = settings or load_settings()
    state = AppState()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        own_pool = pool is None
        p = pool or await db.init_pool(settings.database_url, settings.db_statement_cache_size)
        await db.ensure_schema(p)
        c = client or build_client(settings)
        state.settings, state.client = settings, c
        state.credits = CreditManager(
            p, c, settings.monthly_credit_limit, settings.credit_reserve,
            settings.credit_pause_pct, settings.pending_stale_minutes,
        )
        state.discovery = Discovery(state.credits)
        state.pool = p
        state.guard = guard_for_pool(p, settings)
        state.deleter = RoundDeleter(p)
        state.deleter.start_purge()  # resumes a purge interrupted by a restart
        state.poller = Poller(p, state.credits, state.discovery, state.guard,
                              tick_seconds=settings.poll_tick_seconds)
        recovered = await state.credits.recover_stale_pending()
        if recovered:
            logger.warning("Marked %d stale pending paid calls as 'unknown'", recovered)
        if c.is_mock:
            await _seed_mock_counter(state)
        app.state.app_state = state
        if settings.poller_enabled:
            state.poller.start()
        try:
            yield
        finally:
            if settings.poller_enabled:
                await state.poller.stop()
            await c.aclose()
            if own_pool:
                await db.close_pool()

    app = FastAPI(title="Sharp Odds", lifespan=lifespan)

    async def guarded(coro):
        try:
            return await coro
        except DiscoveryError as exc:
            raise HTTPException(400, str(exc)) from None
        except OddsApiNetworkError as exc:
            raise HTTPException(502, f"Odds API unreachable: {exc}") from None

    @app.get("/api/health")
    async def health():
        return {"ok": True, "mock": state.client.is_mock}

    @app.get("/api/credits")
    async def credits_status():
        b = await state.credits.budget()
        return {
            "mock": state.client.is_mock,
            "budget": b.as_dict(),
            "recent_paid_calls": [_jsonable(r) for r in await state.credits.recent_calls(10)],
        }

    @app.post("/api/credits/sync")
    async def credits_sync():
        """Free /sports call; refreshes the API's real usage counter."""
        b = await state.credits.sync()
        return {"budget": b.as_dict()}

    @app.post("/api/credits/estimate")
    async def credits_estimate(q: CostQuery):
        books = q.bookmakers or list(state.settings.default_bookmakers)
        try:
            return {"per_league_refresh": estimate_cost(q.markets, books)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    # ---- discovery (free listings only) -------------------------------
    @app.get("/api/config")
    async def config():
        st = state.settings
        return {"mock": state.client.is_mock, "default_bookmakers": list(st.default_bookmakers),
                "default_markets": list(st.default_markets), "default_preset": st.schedule_preset}

    @app.get("/api/discovery/leagues")
    async def discovery_leagues(inactive: bool = False, refresh: bool = False):
        return await guarded(state.discovery.leagues(inactive, refresh))

    @app.get("/api/discovery/events")
    async def discovery_events(sports: str = Query(..., min_length=1), refresh: bool = False):
        keys = [s for s in sports.split(",") if s][:60]
        return await guarded(state.discovery.events_for(keys, refresh))

    @app.get("/api/discovery/bookmakers")
    async def discovery_bookmakers():
        return bookmaker_catalog()

    @app.post("/api/discovery/estimate")
    async def discovery_estimate(sel: Selection):
        return await guarded(state.discovery.estimate(
            [m.model_dump() for m in sel.matches], sel.bookmakers, sel.markets, sel.preset))

    @app.post("/api/rounds/add")
    async def rounds_add(body: AddToRound):
        return await guarded(state.discovery.add_to_round(
            body.round_label, [m.model_dump() for m in body.matches], body.bookmakers, body.markets, body.preset))

    @app.get("/api/rounds")
    async def rounds_list():
        return await state.discovery.rounds()

    # ---- dashboard ---------------------------------------------------------
    @app.get("/api/matches")
    async def matches(round_label: str | None = None):
        rows = await dashboard.matches_summary(state.pool, round_label)
        for r in rows:
            upcoming = [p for p in state.poller.plan_for(r["event_id"]) if not p.get("dropped")]
            r["next_snapshot"] = upcoming[0] if upcoming and r["is_monitored"] else None
        return {"version": state.poller.version, "matches": rows}

    @app.get("/api/matches/{matchup_id}")
    async def match(matchup_id: int):
        detail = await dashboard.match_detail(state.pool, matchup_id)
        if detail is None:
            raise HTTPException(404, "no such match")
        detail["plan"] = state.poller.plan_for(detail["match"]["event_id"])
        return detail

    @app.put("/api/matches/{matchup_id}/result")
    async def match_result(matchup_id: int, body: MatchResult):
        try:
            ok = await dashboard.set_result(state.pool, matchup_id, body.home_score, body.away_score)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if not ok:
            raise HTTPException(404, "no such match")
        return {"ok": True}

    @app.put("/api/matches/{matchup_id}/monitor")
    async def match_monitor(matchup_id: int, on: bool):
        if not await dashboard.set_monitored(state.pool, matchup_id, on, state.credits.clock()):
            raise HTTPException(409, "cannot change monitoring (match unknown or already kicked off)")
        state.poller.version += 1
        return {"ok": True}

    @app.get("/api/poller")
    async def poller_status():
        p = state.poller
        plan = p.last_plan
        nxt = None
        if plan is not None and plan.calls:
            c = plan.calls[0]
            nxt = {"sport": c.sport_key, "at": c.time.isoformat(), "cost": c.cost,
                   "labels": sorted({x.label for x in c.points})}
        return {"enabled": settings.poller_enabled, "leader": p.leader, "version": p.version,
                "tick_seconds": p.tick_seconds, "last_tick": _jsonable(p.last_tick or {}),
                "plan": None if plan is None else {
                    "calls_this_month": len(plan.calls_this_month()), "cost_this_month": plan.cost_this_month,
                    "available": plan.available, "dropped_calls": plan.dropped_calls, "next": nxt}}

    @app.get("/api/rounds/refresh-quote")
    async def refresh_quote(round_label: str):
        quote = await state.poller.round_refresh_quote(round_label)
        quote["budget"] = (await state.credits.budget()).as_dict()
        return quote

    @app.post("/api/rounds/refresh")
    async def refresh_round(body: RefreshRequest):
        res = await state.poller.refresh_round(body.round_label, body.expected_cost)
        if not res["ok"]:
            raise HTTPException(409, f"the cost changed to {res['quote']['cost']} credits; check and confirm again")
        return _jsonable(res)

    # ---- storage -------------------------------------------------------------
    @app.get("/api/storage")
    async def storage():
        return await dashboard.storage_overview(state.pool, state.guard)

    @app.get("/api/storage/status")
    async def storage_status():
        return await state.guard.usage(force=True)

    @app.delete("/api/rounds")
    async def delete_round(round_label: str, allow_undownloaded: bool = False):
        async with state.pool.acquire() as con:
            downloaded = await con.fetchval("select 1 from round_downloads where round_label = $1", round_label)
        if not downloaded and not allow_undownloaded:
            raise HTTPException(409, "never_downloaded: this round was never downloaded as CSV; deleting it loses "
                                     "the data for good. Confirm explicitly to delete anyway.")
        n = await state.deleter.hide_round(round_label)
        if n == 0:
            raise HTTPException(404, "no such round")
        if n < 0:
            raise HTTPException(409, "this round still has tracked matches; stop tracking them first")
        state.deleter.start_purge()
        state.poller.version += 1
        return {"ok": True, "deleted_matches": n, "purging_in_background": True}

    @app.post("/api/storage/reclaim")
    async def reclaim():
        """VACUUM FULL the snapshot tables, only when the copy fits under the limit."""
        if await state.deleter.pending_count():
            raise HTTPException(409, "deleted matches are still being removed; try again in a moment")
        async with state.pool.acquire() as con:
            before = int(await con.fetchval("select pg_database_size(current_database())"))
            done, skipped = [], []
            for name in ("odds_snapshots", "matchups", "credit_ledger"):
                need = int(await con.fetchval("select pg_total_relation_size($1::regclass)", name) * 1.1)
                used = int(await con.fetchval("select pg_database_size(current_database())"))
                if used + need > state.guard.limit_bytes * 0.97:
                    skipped.append(name)
                    continue
                await con.execute(f"vacuum full {name}")
                done.append(name)
            after = int(await con.fetchval("select pg_database_size(current_database())"))
        return {"ok": True, "before_bytes": before, "after_bytes": after, "rewritten": done, "skipped": skipped}

    # ---- frontend --------------------------------------------------------
    if FRONTEND.exists():
        app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(FRONTEND / "index.html", headers={"Cache-Control": "no-cache"})

    return app


async def _seed_mock_counter(state: AppState) -> None:
    """The mock API keeps its counter in memory; start it where the ledger
    left off so a restart in mock mode does not 'refund' credits."""
    from app.mock_api import MockOddsApi  # noqa: F401  (type only)

    b = await state.credits.budget()
    transport = getattr(state.client._http, "_transport", None)
    handler = getattr(transport, "handler", None)
    mock = getattr(handler, "__self__", None)
    if mock is not None and hasattr(mock, "used"):
        mock.used = max(mock.used, b.used)


def _jsonable(row: dict) -> dict:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}

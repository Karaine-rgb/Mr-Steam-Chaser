"""FastAPI app. Phase 2: credit status and free re-sync only."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import db
from app.config import Settings, load_settings
from app.credits import CreditManager
from app.odds_api import OddsApiClient, build_client
from app.planner import estimate_cost

logger = logging.getLogger("api")


class AppState:
    settings: Settings
    client: OddsApiClient
    credits: CreditManager


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
        recovered = await state.credits.recover_stale_pending()
        if recovered:
            logger.warning("Marked %d stale pending paid calls as 'unknown'", recovered)
        if c.is_mock:
            await _seed_mock_counter(state)
        app.state.app_state = state
        try:
            yield
        finally:
            await c.aclose()
            if own_pool:
                await db.close_pool()

    app = FastAPI(title="Sharp Odds", lifespan=lifespan)

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

    class CostQuery(BaseModel):
        markets: list[str] = Field(default_factory=lambda: ["h2h"])
        bookmakers: list[str] = Field(default_factory=list)

    @app.post("/api/credits/estimate")
    async def credits_estimate(q: CostQuery):
        books = q.bookmakers or list(state.settings.default_bookmakers)
        try:
            return {"per_league_refresh": estimate_cost(q.markets, books)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

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

"""Environment configuration.

Every value comes from an environment variable. Nothing secret has a default:
ODDS_API_KEY and DATABASE_URL are empty unless set. The API key is read here
and handed to the HTTP client only; it is never logged, stored in the
database or sent to the frontend.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

# Agreed default bookmakers (10 = 1 region-equivalent = 1 credit per market).
# See docs/plan.md section 5. Chosen by the user; "sharpness" is reputation,
# not something the API states.
DEFAULT_BOOKMAKERS = (
    "pinnacle",
    "betfair_ex_eu",
    "matchbook",
    "smarkets",
    "onexbet",
    "marathonbet",
    "williamhill",
    "unibet_uk",
    "betway",
    "betvictor",
)


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _list(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    return tuple(x.strip() for x in value.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    # Supabase pooled connections (PgBouncer-style) do not keep prepared
    # statements between transactions. asyncpg documents 0 as "disable the
    # cache" and treats it as the PgBouncer setting. 0 also works on a direct
    # connection, only slightly slower. Not yet verified against Supabase docs.
    db_statement_cache_size: int = 0

    odds_api_key: str = field(default="", repr=False)
    odds_api_base_url: str = "https://api.the-odds-api.com"
    odds_api_mock: bool = False
    odds_api_timeout_s: float = 30.0

    # Credit budget (docs/plan.md section 1.2).
    monthly_credit_limit: int = 500
    credit_reserve: int = 50
    credit_pause_pct: float = 90.0
    # A paid call still "pending" after this long belongs to a crashed or
    # timed-out request; it is marked "unknown" and counted at its expected cost.
    pending_stale_minutes: float = 5.0

    default_bookmakers: tuple[str, ...] = DEFAULT_BOOKMAKERS
    default_markets: tuple[str, ...] = ("h2h",)
    schedule_preset: str = "balanced"

    # Supabase free tier storage guard (same defaults as the old tool).
    db_limit_mb: float = 500.0
    db_warn_pct: float = 80.0
    db_critical_pct: float = 90.0
    db_pause_pct: float = 95.0

    # Optional HTTP Basic auth for the whole site (except /api/health). Strongly
    # recommended on a public URL: without it anyone with the link can spend
    # credits with "Refresh now" or delete rounds.
    app_user: str = "sharp"
    app_password: str = field(default="", repr=False)

    poller_enabled: bool = True
    poll_tick_seconds: float = 30.0

    port: int = 8000


def load_settings(env: dict[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    return Settings(
        database_url=e.get("DATABASE_URL", ""),
        db_statement_cache_size=int(e.get("DB_STATEMENT_CACHE_SIZE", "0")),
        odds_api_key=e.get("ODDS_API_KEY", ""),
        odds_api_base_url=e.get("ODDS_API_BASE_URL", "https://api.the-odds-api.com").rstrip("/"),
        odds_api_mock=_bool(e.get("ODDS_API_MOCK")),
        odds_api_timeout_s=float(e.get("ODDS_API_TIMEOUT_S", "30")),
        monthly_credit_limit=int(e.get("MONTHLY_CREDIT_LIMIT", "500")),
        credit_reserve=int(e.get("CREDIT_RESERVE", "50")),
        credit_pause_pct=float(e.get("CREDIT_PAUSE_PCT", "90")),
        pending_stale_minutes=float(e.get("PENDING_STALE_MINUTES", "5")),
        default_bookmakers=_list(e.get("DEFAULT_BOOKMAKERS"), DEFAULT_BOOKMAKERS),
        default_markets=_list(e.get("DEFAULT_MARKETS"), ("h2h",)),
        schedule_preset=e.get("SCHEDULE_PRESET", "balanced"),
        db_limit_mb=float(e.get("DB_LIMIT_MB", "500")),
        db_warn_pct=float(e.get("DB_WARN_PCT", "80")),
        db_critical_pct=float(e.get("DB_CRITICAL_PCT", "90")),
        db_pause_pct=float(e.get("DB_PAUSE_PCT", "95")),
        app_user=e.get("APP_USER", "sharp"),
        app_password=e.get("APP_PASSWORD", ""),
        poller_enabled=(e.get("POLLER_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}),
        poll_tick_seconds=float(e.get("POLL_TICK_SECONDS", "30")),
        port=int(e.get("PORT", "8000")),
    )


settings = load_settings()

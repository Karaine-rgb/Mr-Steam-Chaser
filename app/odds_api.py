"""HTTP client for The Odds API v4.

Rules (docs/odds_api_facts.md, docs/plan.md section 2):
  - The key travels in the `x-api-key` header, never in the URL, so it cannot
    leak through URLs, access logs or exception messages.
  - /sports and /events are free. They may retry once after a 429.
  - /odds is PAID. `_get_odds_unchecked` sends exactly one request and never
    retries. Only app.credits.CreditManager.paid_call may call it (a test
    checks that no other module does).
  - While pytest is running, a client that would talk to the real network
    refuses to send anything (RealApiForbidden). Tests use the mock transport.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("odds_api")

KNOWN_ERROR_CODES = (
    "OUT_OF_USAGE_CREDITS",
    "EXCEEDED_FREQ_LIMIT",
    "INVALID_KEY",
    "MISSING_KEY",
    "DEACTIVATED_KEY",
    "INVALID_MARKET",
    "INVALID_BOOKMAKERS",
    "UNKNOWN_SPORT",
    "INVALID_SPORT",
    "EVENT_NOT_FOUND",
)


class RealApiForbidden(RuntimeError):
    """Raised when code under test tries to reach the real API."""


class OddsApiConfigError(RuntimeError):
    pass


class OddsApiNetworkError(RuntimeError):
    """The request failed without an HTTP response.

    `maybe_charged` is False only when the connection was never made (the
    server cannot have seen the request). A timeout after sending is True:
    we cannot know whether credits were taken.
    """

    def __init__(self, message: str, maybe_charged: bool):
        super().__init__(message)
        self.maybe_charged = maybe_charged


def _int_header(headers: httpx.Headers, name: str) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


@dataclass(frozen=True)
class Usage:
    remaining: int | None = None
    used: int | None = None
    last: int | None = None

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> "Usage":
        return cls(
            remaining=_int_header(headers, "x-requests-remaining"),
            used=_int_header(headers, "x-requests-used"),
            last=_int_header(headers, "x-requests-last"),
        )


@dataclass
class ApiResult:
    status: int
    data: Any
    usage: Usage
    error_code: str | None = None
    message: str | None = None
    endpoint: str = ""
    params: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def event_count(self) -> int | None:
        return len(self.data) if self.ok and isinstance(self.data, list) else None


def _error_info(response: httpx.Response) -> tuple[str | None, str | None]:
    """The body format of API errors is not documented beyond "will contain
    more info", so look for a known code anywhere in it."""
    text = response.text or ""
    message = None
    try:
        body = response.json()
        if isinstance(body, dict):
            message = body.get("message")
            code = body.get("error_code")
            if code:
                return str(code), message
    except (json.JSONDecodeError, ValueError):
        pass
    for code in KNOWN_ERROR_CODES:
        if code in text:
            return code, message or text[:300]
    return None, message or text[:300] or None


class OddsApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        is_mock: bool = False,
    ) -> None:
        self.is_mock = is_mock
        self._real_network = transport is None
        if self._real_network and not api_key:
            raise OddsApiConfigError("ODDS_API_KEY is not set (or set ODDS_API_MOCK=1)")
        headers = {"x-api-key": api_key} if api_key else {}
        self._http = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=timeout_s, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _guard(self) -> None:
        if self._real_network and "PYTEST_CURRENT_TEST" in os.environ:
            raise RealApiForbidden("Tests must never call the real Odds API")

    async def _send(self, path: str, params: dict) -> ApiResult:
        self._guard()
        clean = {k: v for k, v in params.items() if v is not None}
        try:
            response = await self._http.get(path, params=clean)
        except httpx.ConnectError as exc:
            raise OddsApiNetworkError(f"connect failed: {type(exc).__name__}", maybe_charged=False) from None
        except httpx.TransportError as exc:
            raise OddsApiNetworkError(f"transport error: {type(exc).__name__}", maybe_charged=True) from None
        usage = Usage.from_headers(response.headers)
        if 200 <= response.status_code < 300:
            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError):
                data = None
            return ApiResult(response.status_code, data, usage, endpoint=path, params=clean)
        code, message = _error_info(response)
        return ApiResult(response.status_code, None, usage, code, message, endpoint=path, params=clean)

    async def _free(self, path: str, params: dict) -> ApiResult:
        result = await self._send(path, params)
        if result.status == 429:
            # Free endpoint: one polite retry is allowed (docs: retry after a
            # couple of seconds). Paid calls never retry.
            await asyncio.sleep(2.0)
            result = await self._send(path, params)
        return result

    # ---- free endpoints -------------------------------------------------
    async def get_sports(self, all_sports: bool = True) -> ApiResult:
        return await self._free("/v4/sports/", {"all": "true" if all_sports else None})

    async def get_events(
        self, sport: str, commence_from: str | None = None, commence_to: str | None = None
    ) -> ApiResult:
        return await self._free(
            f"/v4/sports/{sport}/events",
            {"commenceTimeFrom": commence_from, "commenceTimeTo": commence_to},
        )

    # ---- PAID endpoint: only app.credits may call this -------------------
    async def _get_odds_unchecked(
        self,
        sport: str,
        markets: list[str],
        bookmakers: list[str],
        event_ids: list[str] | None = None,
        commence_from: str | None = None,
        commence_to: str | None = None,
    ) -> ApiResult:
        return await self._send(
            f"/v4/sports/{sport}/odds",
            {
                "markets": ",".join(markets),
                "bookmakers": ",".join(bookmakers),
                "oddsFormat": "decimal",
                "dateFormat": "iso",
                "eventIds": ",".join(event_ids) if event_ids else None,
                "commenceTimeFrom": commence_from,
                "commenceTimeTo": commence_to,
            },
        )


def build_client(settings) -> OddsApiClient:
    if settings.odds_api_mock:
        from app.mock_api import MockOddsApi

        mock = MockOddsApi(quota=settings.monthly_credit_limit)
        return OddsApiClient(
            settings.odds_api_base_url, "", settings.odds_api_timeout_s,
            transport=httpx.MockTransport(mock.handle), is_mock=True,
        )
    return OddsApiClient(settings.odds_api_base_url, settings.odds_api_key, settings.odds_api_timeout_s)

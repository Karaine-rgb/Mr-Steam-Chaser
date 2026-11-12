"""Mock Odds API for ODDS_API_MOCK=1 and for every test. Costs nothing.

Data:
  - app/mock_data/live/*.json are REAL responses recorded on 2026-11-12
    (sports list, EPL and Bundesliga events, one EPL h2h odds call).
  - Timestamps are shifted so the recorded fixtures always look as far in the
    future as they did when recorded. The UI keeps working on any date.
  - Leagues with recorded events but no recorded odds (for example the
    Bundesliga) get SYNTHETIC h2h odds made up from the team names. They are
    plausible numbers for UI work only, not real prices.
  - Each /odds call moves some prices slightly (deterministic random walk),
    so snapshot de-duplication and charts have something to show.

Cost rules follow the docs: markets x ceil(bookmakers / 10) (or number of
regions), and 0 when no events come back. The usage headers mimic the real
ones. The 401 body for "out of credits" is an ASSUMED shape: the real body
format is not documented.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

DATA_DIR = Path(__file__).parent / "mock_data" / "live"
RECORDED_AT = datetime(2026, 11, 12, 9, 43, 56, tzinfo=timezone.utc)  # first recording

SYNTHETIC_BOOKS = ("pinnacle", "betfair_ex_eu", "onexbet", "marathonbet", "unibet_uk", "williamhill", "betvictor")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _load(name: str):
    path = DATA_DIR / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _recorded_at(name: str) -> datetime:
    """When a fixture file was recorded (recordings.json), so each file is
    shifted by its own age."""
    times = _load("recordings.json") or {}
    value = times.get(name)
    return _parse(value) if value else RECORDED_AT


class MockOddsApi:
    def __init__(self, quota: int = 500, clock: Callable[[], datetime] | None = None, used: int = 0) -> None:
        self.quota = quota
        self.used = used
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.odds_calls = 0
        self.requests: list[httpx.Request] = []

    # -- helpers ------------------------------------------------------
    def _offset(self, name: str) -> timedelta:
        return self.clock() - _recorded_at(name)

    def _shift(self, value: str, name: str) -> str:
        return _iso(_parse(value) + self._offset(name))

    def _headers(self, last: int) -> dict:
        return {
            "x-requests-last": str(last),
            "x-requests-used": str(self.used),
            "x-requests-remaining": str(max(0, self.quota - self.used)),
        }

    def _json(self, status: int, body, last: int = 0) -> httpx.Response:
        return httpx.Response(status, json=body, headers=self._headers(last))

    def _events(self, sport: str) -> list[dict] | None:
        name = f"events_{sport}.json"
        events = _load(name)
        if events is None:
            name = f"odds_{sport}_h2h.json"
            odds = _load(name)
            if odds is None:
                return None
            events = [{k: v for k, v in e.items() if k != "bookmakers"} for e in odds]
        out = []
        for e in events:
            e = dict(e)
            e["commence_time"] = self._shift(e["commence_time"], name)
            out.append(e)
        return out

    def _synthetic_book(self, event: dict, book: str) -> dict:
        rnd = random.Random(f"{event['home_team']}|{event['away_team']}")
        strength = rnd.uniform(-1.2, 1.2) + 0.25  # home advantage
        p_home = 1 / (1 + math.exp(-strength)) * 0.75
        p_draw = 0.27 - abs(strength) * 0.04
        p_away = 1 - p_home - p_draw
        margin = random.Random(book).uniform(0.02, 0.07)
        price = lambda p: round(1 / (p * (1 + margin)), 2)
        ts = event["commence_time"]  # replaced below
        return {
            "key": book, "title": book, "last_update": ts,
            "markets": [{
                "key": "h2h", "last_update": ts,
                "outcomes": [
                    {"name": event["away_team"], "price": price(p_away)},
                    {"name": event["home_team"], "price": price(p_home)},
                    {"name": "Draw", "price": price(p_draw)},
                ],
            }],
        }

    def _odds(self, sport: str) -> list[dict] | None:
        recorded = _load(f"odds_{sport}_h2h.json")
        now_iso = _iso(self.clock())
        if recorded is not None:
            out = []
            for e in recorded:
                e = json.loads(json.dumps(e))
                e["commence_time"] = self._shift(e["commence_time"], f"odds_{sport}_h2h.json")
                for b in e.get("bookmakers", []):
                    b["last_update"] = now_iso
                    for m in b.get("markets", []):
                        m["last_update"] = now_iso
                out.append(e)
            return out
        events = self._events(sport)
        if events is None:
            return None
        out = []
        for e in events:
            e = dict(e)
            books = [self._synthetic_book(e, b) for b in SYNTHETIC_BOOKS]
            for b in books:
                b["last_update"] = now_iso
                b["markets"][0]["last_update"] = now_iso
            e["bookmakers"] = books
            out.append(e)
        return out

    def _drift(self, events: list[dict]) -> None:
        """Deterministic random walk on home/away prices, one step per call."""
        n = self.odds_calls
        for e in events:
            for b in e.get("bookmakers", []):
                walk = 0.0
                for k in range(1, n + 1):
                    r = random.Random(f"{e['id']}|{b['key']}|{k}").random()
                    walk += 0.02 if r < 0.15 else (-0.02 if r > 0.85 else 0.0)
                if walk == 0.0:
                    continue
                for m in b.get("markets", []):
                    for o in m.get("outcomes", []):
                        if o["name"] == e["home_team"]:
                            o["price"] = round(max(1.01, o["price"] * (1 + walk)), 2)
                        elif o["name"] == e["away_team"]:
                            o["price"] = round(max(1.01, o["price"] * (1 - walk)), 2)

    @staticmethod
    def _time_filter(events: list[dict], params) -> list[dict]:
        lo, hi = params.get("commenceTimeFrom"), params.get("commenceTimeTo")
        ids = params.get("eventIds")
        if ids:
            wanted = set(ids.split(","))
            events = [e for e in events if e["id"] in wanted]
        if lo:
            events = [e for e in events if _parse(e["commence_time"]) >= _parse(lo)]
        if hi:
            events = [e for e in events if _parse(e["commence_time"]) <= _parse(hi)]
        return events

    # -- transport entry point ----------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.rstrip("/")
        params = request.url.params
        parts = path.split("/")  # ['', 'v4', 'sports', sport, endpoint]

        if path == "/v4/sports":
            sports = _load("sports_all.json") or []
            if params.get("all") != "true":
                sports = [s for s in sports if s.get("active")]
            return self._json(200, sports)

        if len(parts) == 5 and parts[1] == "v4" and parts[2] == "sports":
            sport, endpoint = parts[3], parts[4]
            if endpoint == "events":
                events = self._events(sport)
                if events is None:
                    return self._json(404, {"message": "Unknown sport (mock)", "error_code": "UNKNOWN_SPORT"})
                return self._json(200, self._time_filter(events, params))
            if endpoint == "odds":
                return self._handle_odds(sport, params)

        return self._json(404, {"message": f"Mock has no route for {path}"})

    def _handle_odds(self, sport: str, params) -> httpx.Response:
        markets = [m for m in (params.get("markets") or "h2h").split(",") if m]
        books = [b for b in (params.get("bookmakers") or "").split(",") if b]
        regions = [r for r in (params.get("regions") or "").split(",") if r]
        if not books and not regions:
            return self._json(422, {"message": "Missing regions (mock)", "error_code": "MISSING_REGION"})
        cost = len(markets) * (math.ceil(len(books) / 10) if books else len(regions))
        if self.used + cost > self.quota:
            return self._json(401, {"message": "Usage quota reached (mock, assumed body shape)",
                                    "error_code": "OUT_OF_USAGE_CREDITS"})
        events = self._odds(sport)
        if events is None:
            return self._json(404, {"message": "Unknown sport (mock)", "error_code": "UNKNOWN_SPORT"})
        events = self._time_filter(events, params)
        self._drift(events)
        for e in events:
            if books:
                e["bookmakers"] = [b for b in e.get("bookmakers", []) if b["key"] in books]
            for b in e.get("bookmakers", []):
                b["markets"] = [m for m in b.get("markets", []) if m["key"] in markets
                                or (m["key"] == "h2h_lay" and "h2h" in markets)]
        self.odds_calls += 1
        charged = cost if events else 0  # docs: no events returned -> no charge
        self.used += charged
        return self._json(200, events, last=charged)

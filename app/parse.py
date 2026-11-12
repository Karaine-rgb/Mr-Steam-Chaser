"""Turn an /odds response into narrow snapshot rows.

Facts this relies on (docs/odds_api_facts.md):
  - Outcomes are NOT in home/draw/away order. The live EPL response listed
    "Fulham, Manchester City, Draw" for Man City (home) v Fulham. Every outcome
    is matched by NAME against home_team / away_team / "Draw".
  - Soccer h2h is 3-way with a "Draw" outcome (verified live).
  - The market-level last_update is the one to use; the bookmaker-level one
    is deprecated.
  - spreads / totals carry a numeric `point`. Whether soccer uses quarter lines
    is NOT verified, so any decimal point value is accepted as-is.
  - Markets can vanish (suspended markets drop out after ~15 min), bookmakers
    can be missing for some events, and new market keys may appear. All of
    these are skipped quietly, never treated as errors.
  - h2h_lay (exchange lay prices) is ignored: only back prices are stored.

Slot convention (same as the old tool's export): for totals, Over goes in the
home slot and Under in the away slot; `point` is the totals line. For spreads,
`point` is the HOME team's handicap.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.odds import fair_probs_from_decimal_odds

logger = logging.getLogger("parse")

SUPPORTED_MARKETS = ("h2h", "spreads", "totals")


@dataclass(frozen=True)
class SnapshotRow:
    event_id: str
    bookmaker: str
    market: str
    market_last_update: datetime | None
    home_price: float | None
    draw_price: float | None
    away_price: float | None
    point: float | None
    fair_home: float | None
    fair_draw: float | None
    fair_away: float | None

    def price_key(self) -> tuple:
        """What has to change for a new snapshot to be worth storing."""
        return (self.home_price, self.draw_price, self.away_price, self.point)


def parse_time(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):  # dateFormat=unix
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _price(o: dict) -> float | None:
    try:
        p = float(o["price"])
    except (KeyError, TypeError, ValueError):
        return None
    return p if p > 1.0 else None


def _point(o: dict) -> float | None:
    if o.get("point") is None:
        return None
    try:
        return float(o["point"])
    except (TypeError, ValueError):
        return None


def _fair(prices: list[float]) -> list[float] | None:
    try:
        return fair_probs_from_decimal_odds(prices)
    except ValueError:
        return None


def _h2h(event: dict, outcomes: list[dict]) -> tuple | None:
    home, away = event.get("home_team"), event.get("away_team")
    by_name: dict[str, float] = {}
    for o in outcomes:
        p = _price(o)
        if p is None:
            return None
        by_name[o.get("name")] = p
    if set(by_name) - {home, away, "Draw"}:
        return None  # a name we cannot place: refuse rather than guess
    h, a, d = by_name.get(home), by_name.get(away), by_name.get("Draw")
    if h is None or a is None:
        return None
    prices = [h, d, a] if d is not None else [h, a]
    fair = _fair(prices)
    if fair is None:
        return None
    if d is not None:
        return h, d, a, None, fair[0], fair[1], fair[2]
    return h, None, a, None, fair[0], None, fair[1]


def _spreads(event: dict, outcomes: list[dict]) -> list[tuple]:
    home, away = event.get("home_team"), event.get("away_team")
    homes = [o for o in outcomes if o.get("name") == home]
    aways = [o for o in outcomes if o.get("name") == away]
    rows = []
    for ho in homes:
        hp, hpt = _price(ho), _point(ho)
        if hp is None or hpt is None:
            continue
        # The matching away outcome carries the opposite handicap.
        match = next((ao for ao in aways if _point(ao) is not None and abs(_point(ao) + hpt) < 1e-9), None)
        if match is None or _price(match) is None:
            continue
        ap = _price(match)
        fair = _fair([hp, ap])
        if fair is None:
            continue
        rows.append((hp, None, ap, hpt, fair[0], None, fair[1]))
    return rows


def _totals(outcomes: list[dict]) -> list[tuple]:
    overs = [o for o in outcomes if o.get("name") == "Over"]
    unders = [o for o in outcomes if o.get("name") == "Under"]
    rows = []
    for ov in overs:
        op, pt = _price(ov), _point(ov)
        if op is None or pt is None:
            continue
        un = next((u for u in unders if _point(u) is not None and abs(_point(u) - pt) < 1e-9), None)
        if un is None or _price(un) is None:
            continue
        up = _price(un)
        fair = _fair([op, up])
        if fair is None:
            continue
        rows.append((op, None, up, pt, fair[0], None, fair[1]))
    return rows


def parse_odds_response(events: list[dict], event_ids: set[str] | None = None) -> list[SnapshotRow]:
    """All usable rows in an /odds response, optionally only for `event_ids`."""
    out: list[SnapshotRow] = []
    if not isinstance(events, list):
        return out
    for event in events:
        eid = event.get("id")
        if not eid or (event_ids is not None and eid not in event_ids):
            continue
        for book in event.get("bookmakers") or []:
            bkey = book.get("key")
            if not bkey:
                continue
            for market in book.get("markets") or []:
                mkey = market.get("key")
                if mkey not in SUPPORTED_MARKETS:
                    continue  # h2h_lay, outrights, future market types
                outcomes = market.get("outcomes") or []
                updated = parse_time(market.get("last_update"))
                if mkey == "h2h":
                    r = _h2h(event, outcomes)
                    values = [r] if r else []
                    if not r:
                        logger.info("Skipped h2h for %s at %s: outcomes could not be placed", eid, bkey)
                elif mkey == "spreads":
                    values = _spreads(event, outcomes)
                else:
                    values = _totals(outcomes)
                for hp, dp, ap, pt, fh, fd, fa in values:
                    out.append(SnapshotRow(eid, bkey, mkey, updated, hp, dp, ap, pt, fh, fd, fa))
    return out

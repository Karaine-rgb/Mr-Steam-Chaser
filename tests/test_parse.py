"""API parsing. The first group uses the REAL recorded EPL response; the
second group uses HAND-WRITTEN events for cases not seen live (quarter lines,
missing bookmakers, empty events, odd outcome names)."""
import json
from pathlib import Path

import pytest

from app.parse import parse_odds_response

LIVE = Path(__file__).resolve().parent.parent / "app" / "mock_data" / "live"


@pytest.fixture(scope="module")
def epl():
    return json.loads((LIVE / "odds_soccer_epl_h2h.json").read_text())


# ---- real recorded data ---------------------------------------------------

def test_live_epl_every_h2h_market_is_placed(epl):
    rows = parse_odds_response(epl)
    expected = sum(1 for e in epl for b in e["bookmakers"] for m in b["markets"] if m["key"] == "h2h")
    assert len(rows) == expected == 130
    assert {r.market for r in rows} == {"h2h"}  # h2h_lay ignored


def test_live_outcomes_mapped_by_name_not_position(epl):
    # Recorded order was Fulham, Manchester City, Draw for Man City (home) v Fulham.
    event = next(e for e in epl if e["home_team"] == "Manchester City")
    pin = next(b for b in event["bookmakers"] if b["key"] == "pinnacle")
    assert [o["name"] for o in pin["markets"][0]["outcomes"]][0] == "Fulham"
    row = next(r for r in parse_odds_response(epl) if r.event_id == event["id"] and r.bookmaker == "pinnacle")
    assert (row.home_price, row.draw_price, row.away_price) == (1.43, 5.18, 6.61)
    assert row.fair_home > row.fair_draw > row.fair_away
    assert row.fair_home + row.fair_draw + row.fair_away == pytest.approx(1.0, abs=1e-6)
    assert row.point is None
    assert row.market_last_update is not None and row.market_last_update.tzinfo is not None


def test_live_event_filter(epl):
    wanted = {epl[0]["id"], epl[1]["id"]}
    rows = parse_odds_response(epl, event_ids=wanted)
    assert rows and {r.event_id for r in rows} == wanted


# ---- hand-written cases ---------------------------------------------------

def _event(bookmakers, eid="e" * 32, home="Home FC", away="Away FC"):
    return {"id": eid, "sport_key": "soccer_test", "commence_time": "2026-12-01T15:00:00Z",
            "home_team": home, "away_team": away, "bookmakers": bookmakers}


def _book(key, markets):
    return {"key": key, "title": key, "last_update": "2026-11-30T10:00:00Z", "markets": markets}


def _market(key, outcomes, ts="2026-11-30T10:00:00Z"):
    return {"key": key, "last_update": ts, "outcomes": outcomes}


def test_empty_responses():
    assert parse_odds_response([]) == []
    assert parse_odds_response(None) == []
    assert parse_odds_response([_event([])]) == []  # event with no bookmakers yet


def test_missing_bookmaker_and_missing_markets():
    events = [_event([_book("pinnacle", []), _book("onexbet", [_market("h2h", [
        {"name": "Home FC", "price": 2.0}, {"name": "Away FC", "price": 3.5}, {"name": "Draw", "price": 3.4}])])])]
    rows = parse_odds_response(events)
    assert [r.bookmaker for r in rows] == ["onexbet"]


def test_unknown_outcome_name_is_skipped_not_guessed():
    events = [_event([_book("x", [_market("h2h", [
        {"name": "Home United", "price": 2.0}, {"name": "Away FC", "price": 3.5}, {"name": "Draw", "price": 3.4}])])])]
    assert parse_odds_response(events) == []


def test_two_way_h2h_without_draw():
    events = [_event([_book("x", [_market("h2h", [{"name": "Away FC", "price": 1.8}, {"name": "Home FC", "price": 2.1}])])])]
    (row,) = parse_odds_response(events)
    assert (row.home_price, row.draw_price, row.away_price) == (2.1, None, 1.8)
    assert row.fair_draw is None and row.fair_home + row.fair_away == pytest.approx(1.0)


def test_bad_price_skips_market():
    events = [_event([_book("x", [_market("h2h", [
        {"name": "Home FC", "price": 1.0}, {"name": "Away FC", "price": 3.5}, {"name": "Draw", "price": 3.4}])])])]
    assert parse_odds_response(events) == []


def test_quarter_line_spreads():
    events = [_event([_book("x", [_market("spreads", [
        {"name": "Away FC", "price": 1.95, "point": 0.25}, {"name": "Home FC", "price": 1.90, "point": -0.25}])])])]
    (row,) = parse_odds_response(events)
    assert row.market == "spreads" and row.point == -0.25
    assert (row.home_price, row.away_price) == (1.90, 1.95)


def test_spreads_mismatched_points_skipped():
    events = [_event([_book("x", [_market("spreads", [
        {"name": "Away FC", "price": 1.95, "point": 0.5}, {"name": "Home FC", "price": 1.90, "point": -0.25}])])])]
    assert parse_odds_response(events) == []


def test_quarter_line_totals_over_in_home_slot():
    events = [_event([_book("x", [_market("totals", [
        {"name": "Under", "price": 1.85, "point": 2.25}, {"name": "Over", "price": 2.0, "point": 2.25}])])])]
    (row,) = parse_odds_response(events)
    assert (row.home_price, row.away_price, row.point) == (2.0, 1.85, 2.25)


def test_lay_and_unknown_markets_ignored():
    events = [_event([_book("betfair_ex_eu", [
        _market("h2h_lay", [{"name": "Home FC", "price": 2.1}, {"name": "Away FC", "price": 3.6}, {"name": "Draw", "price": 3.5}]),
        _market("some_new_market", [{"name": "Yes", "price": 1.5}]),
    ])])]
    assert parse_odds_response(events) == []


def test_unix_timestamps():
    ev = _event([_book("x", [_market("h2h", [{"name": "Home FC", "price": 2.0}, {"name": "Away FC", "price": 3.5},
                                             {"name": "Draw", "price": 3.4}], ts=1764496800)])])
    (row,) = parse_odds_response([ev])
    assert row.market_last_update.year == 2025

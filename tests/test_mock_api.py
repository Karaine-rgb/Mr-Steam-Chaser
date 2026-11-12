"""Mock mode follows the documented cost rules and stays usable on any date."""
from datetime import datetime, timedelta, timezone

from app.mock_api import RECORDED_AT
from app.parse import parse_odds_response


async def test_sports_free_and_active_filter(client):
    all_sports = await client.get_sports(True)
    active = await client.get_sports(False)
    assert all_sports.usage.last == 0 and len(all_sports.data) == 187
    assert 0 < len(active.data) < 187


async def test_events_are_time_shifted(client, clock):
    clock.now = RECORDED_AT + timedelta(days=100)
    res = await client.get_events("soccer_epl")
    first = min(datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) for e in res.data)
    assert first > clock.now  # still in the future, 100 days later
    assert res.usage.last == 0


async def test_odds_cost_rules(client, mock_api):
    books10 = [f"b{i}" for i in range(9)] + ["pinnacle"]
    r = await client._get_odds_unchecked("soccer_epl", ["h2h"], books10)
    assert r.usage.last == 1
    r = await client._get_odds_unchecked("soccer_epl", ["h2h", "totals"], books10 + ["x"])
    assert r.usage.last == 4  # 2 markets x 2 region-equivalents
    assert mock_api.used == 5


async def test_odds_filters_and_zero_cost_when_empty(client, mock_api):
    events = (await client.get_events("soccer_epl")).data
    one = await client._get_odds_unchecked("soccer_epl", ["h2h"], ["pinnacle"], event_ids=[events[0]["id"]])
    assert [e["id"] for e in one.data] == [events[0]["id"]]
    assert {b["key"] for b in one.data[0]["bookmakers"]} == {"pinnacle"}
    far = (datetime.now(timezone.utc) + timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%SZ")
    empty = await client._get_odds_unchecked("soccer_epl", ["h2h"], ["pinnacle"], commence_from=far)
    assert empty.data == [] and empty.usage.last == 0


async def test_synthetic_odds_for_league_without_recording(client):
    r = await client._get_odds_unchecked("soccer_germany_bundesliga", ["h2h"], ["pinnacle", "onexbet"])
    rows = parse_odds_response(r.data)
    assert len(rows) == 2 * len(r.data) and all(row.draw_price for row in rows)


async def test_prices_drift_between_calls(client):
    first = parse_odds_response((await client._get_odds_unchecked("soccer_epl", ["h2h"], ["pinnacle"])).data)
    changed = False
    for _ in range(5):
        later = parse_odds_response((await client._get_odds_unchecked("soccer_epl", ["h2h"], ["pinnacle"])).data)
        if [r.price_key() for r in later] != [r.price_key() for r in first]:
            changed = True
    assert changed


async def test_unknown_sport_and_quota(client, mock_api):
    assert (await client.get_events("soccer_nowhere")).status == 404
    mock_api.used = 500
    r = await client._get_odds_unchecked("soccer_epl", ["h2h"], ["pinnacle"])
    assert r.status == 401 and r.error_code == "OUT_OF_USAGE_CREDITS"

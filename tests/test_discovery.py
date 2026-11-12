"""Discovery: cached free listings, validation, cost estimate, add to round.
The overarching rule: nothing here may make a paid call."""
from datetime import timedelta

import httpx
import pytest

from app.api import create_app
from app.catalog import country_of, soccer_leagues
from app.config import DEFAULT_BOOKMAKERS, load_settings
from app.discovery import Discovery, DiscoveryCache, DiscoveryError

BOOKS = list(DEFAULT_BOOKMAKERS)


@pytest.fixture
def discovery(make_manager):
    return Discovery(make_manager(), DiscoveryCache())


async def paid_rows(pool):
    async with pool.acquire() as con:
        return await con.fetchval("select count(*) from credit_ledger where is_paid")


# ---- catalogue -----------------------------------------------------------

@pytest.mark.parametrize("key,country", [
    ("soccer_epl", "England"), ("soccer_efl_champ", "England"), ("soccer_fa_cup", "England"),
    ("soccer_spl", "Scotland"), ("soccer_league_of_ireland", "Ireland"), ("soccer_korea_kleague1", "South Korea"),
    ("soccer_uefa_champs_league", "International"), ("soccer_saudi_arabia_pro_league", "Saudi Arabia"),
    ("soccer_germany_bundesliga2", "Germany"), ("soccer_atlantis_premier", "Other"),
])
def test_country_mapping(key, country):
    assert country_of(key) == country


def test_outright_only_keys_are_excluded():
    sports = [{"key": "soccer_fifa_world_cup_winner", "group": "Soccer", "active": True, "has_outrights": True},
              {"key": "soccer_epl", "group": "Soccer", "active": True, "has_outrights": False, "title": "EPL"},
              {"key": "basketball_nba", "group": "Basketball", "active": True}]
    assert [l["key"] for l in soccer_leagues(sports)] == ["soccer_epl"]


# ---- caching: repeated clicks cost nothing and do not even hit the API ----

async def test_leagues_cached(discovery, mock_api):
    first = await discovery.leagues()
    assert len(first["leagues"]) == 45 and all(l["active"] for l in first["leagues"])
    n = len(mock_api.requests)
    await discovery.leagues()
    await discovery.leagues()
    assert len(mock_api.requests) == n
    await discovery.leagues(refresh=True)
    assert len(mock_api.requests) == n + 1
    assert len((await discovery.leagues(include_inactive=True))["leagues"]) == 67


async def test_events_cached_and_expire(discovery, mock_api):
    await discovery.events_for(["soccer_epl"])
    n = len(mock_api.requests)
    await discovery.events_for(["soccer_epl"])
    assert len(mock_api.requests) == n
    discovery.cache.clock = lambda: 10**9  # far later: cache expired
    await discovery.events_for(["soccer_epl"])
    assert len(mock_api.requests) > n


async def test_events_drop_started_matches_and_flag_tracked(discovery, clock):
    data = await discovery.events_for(["soccer_epl", "soccer_germany_bundesliga"])
    assert len(data["events"]) == 38
    first = data["events"][0]
    await discovery.add_to_round("R1", [first], BOOKS, ["h2h"], "balanced")
    again = await discovery.events_for(["soccer_epl", "soccer_germany_bundesliga"])
    assert next(e for e in again["events"] if e["event_id"] == first["event_id"])["tracked_in"] == "R1"
    clock.now += timedelta(days=9, hours=3)  # past the first round of kickoffs
    later = await discovery.events_for(["soccer_epl"])
    assert len(later["events"]) < 20


async def test_unknown_league_rejected(discovery):
    with pytest.raises(DiscoveryError):
        await discovery.events_for(["soccer_atlantis"])


# ---- estimate --------------------------------------------------------------

async def test_estimate_two_leagues(discovery, pool):
    ev = (await discovery.events_for(["soccer_epl", "soccer_germany_bundesliga"]))["events"]
    picks = [e for e in ev if e["sport_key"] == "soccer_epl"][:3] + \
            [e for e in ev if e["sport_key"] == "soccer_germany_bundesliga"][:2]
    est = await discovery.estimate(picks, BOOKS, ["h2h"], "balanced")
    assert est["matches"] == 5 and est["leagues"] == 2
    assert est["per_refresh"] == 2 and est["cost_per_league_refresh"] == 1
    assert 2 <= est["this_month"] <= 20 and est["fits"]
    more = await discovery.estimate(picks, BOOKS, ["h2h", "totals"], "balanced")
    assert more["per_refresh"] == 4 and more["this_month"] == 2 * est["this_month"]
    assert await paid_rows(pool) == 0


@pytest.mark.parametrize("books,markets,preset,msg", [
    ([], ["h2h"], "balanced", "at least one"),
    (["bet365_au"], ["h2h"], "balanced", "free plan"),
    (["circa"], ["h2h"], "balanced", "unknown bookmaker"),
    (BOOKS, ["totals"], "balanced", "must include h2h"),
    (BOOKS, ["h2h", "btts"], "balanced", "must include h2h"),
    (BOOKS, ["h2h"], "hourly", "unknown schedule"),
])
def test_selection_validation(books, markets, preset, msg):
    with pytest.raises(DiscoveryError, match=msg):
        Discovery.check_selection(books, markets, preset)


# ---- add to round ----------------------------------------------------------

async def test_add_uses_server_data_not_browser_data(discovery, pool):
    ev = (await discovery.events_for(["soccer_epl"]))["events"][0]
    forged = {**ev, "home_team": "Forged FC", "commence_time": "2030-01-01T00:00:00Z"}
    res = await discovery.add_to_round("  Weekend 1 ", [forged], BOOKS, ["h2h"], "lean")
    assert res == {"round_label": "Weekend 1", "added": 1, "already_in_round": 0, "credits_spent": 0}
    async with pool.acquire() as con:
        row = await con.fetchrow("select * from matchups")
        rs = await con.fetchrow("select * from round_settings")
    assert row["home_team"] == ev["home_team"] and row["is_monitored"]
    assert row["league_title"] == "EPL"
    assert list(rs["bookmakers"]) == BOOKS and rs["schedule_preset"] == "lean"
    assert await paid_rows(pool) == 0


async def test_add_is_idempotent_and_rejects_other_round(discovery):
    ev = (await discovery.events_for(["soccer_epl"]))["events"][:2]
    await discovery.add_to_round("A", ev, BOOKS, ["h2h"], "balanced")
    again = await discovery.add_to_round("A", ev, BOOKS, ["h2h"], "balanced")
    assert again["added"] == 0 and again["already_in_round"] == 2
    with pytest.raises(DiscoveryError, match="another round"):
        await discovery.add_to_round("B", ev[:1], BOOKS, ["h2h"], "balanced")


async def test_add_rejects_bad_input(discovery):
    ev = (await discovery.events_for(["soccer_epl"]))["events"][0]
    with pytest.raises(DiscoveryError, match="label"):
        await discovery.add_to_round("   ", [ev], BOOKS, ["h2h"], "balanced")
    with pytest.raises(DiscoveryError, match="label"):
        await discovery.add_to_round("__deleting__", [ev], BOOKS, ["h2h"], "balanced")
    with pytest.raises(DiscoveryError, match="bad event id"):
        await discovery.add_to_round("R", [{"event_id": "x';drop table matchups;--", "sport_key": "soccer_epl"}],
                                     BOOKS, ["h2h"], "balanced")
    with pytest.raises(DiscoveryError, match="no longer listed"):
        await discovery.add_to_round("R", [{"event_id": "0" * 32, "sport_key": "soccer_epl"}],
                                     BOOKS, ["h2h"], "balanced")
    with pytest.raises(DiscoveryError, match="no matches"):
        await discovery.add_to_round("R", [], BOOKS, ["h2h"], "balanced")


async def test_add_rejects_started_match(discovery, clock):
    ev = (await discovery.events_for(["soccer_epl"]))["events"][0]
    from app.parse import parse_time
    clock.now = parse_time(ev["commence_time"]) + timedelta(minutes=1)
    with pytest.raises(DiscoveryError, match="kicked off"):
        await discovery.add_to_round("R", [ev], BOOKS, ["h2h"], "balanced")


# ---- HTTP layer --------------------------------------------------------------

async def test_http_discovery_flow_spends_nothing(pool, client, mock_api):
    app = create_app(load_settings({"ODDS_API_MOCK": "1"}), client=client, pool=pool)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            assert (await http.get("/api/config")).json()["default_bookmakers"] == BOOKS
            leagues = (await http.get("/api/discovery/leagues")).json()["leagues"]
            assert any(l["popular"] for l in leagues)
            books = (await http.get("/api/discovery/bookmakers")).json()["bookmakers"]
            assert {b["key"] for b in books} >= set(BOOKS)
            ev = (await http.get("/api/discovery/events", params={"sports": "soccer_epl"})).json()["events"]
            sel = {"matches": [{"event_id": e["event_id"], "sport_key": e["sport_key"]} for e in ev[:4]],
                   "bookmakers": BOOKS, "markets": ["h2h"], "preset": "balanced"}
            est = (await http.post("/api/discovery/estimate", json=sel)).json()
            assert est["per_refresh"] == 1 and est["matches"] == 4
            bad = await http.post("/api/discovery/estimate", json={**sel, "bookmakers": ["bet365_au"]})
            assert bad.status_code == 400 and "free plan" in bad.json()["detail"]
            added = (await http.post("/api/rounds/add", json={**sel, "round_label": "Test round"})).json()
            assert added["added"] == 4 and added["credits_spent"] == 0
            rounds = (await http.get("/api/rounds")).json()
            assert rounds[0]["round_label"] == "Test round" and rounds[0]["matches"] == 4
            assert (await http.get("/api/discovery/events", params={"sports": "soccer_atlantis"})).status_code == 400
    assert mock_api.odds_calls == 0
    assert await paid_rows(pool) == 0

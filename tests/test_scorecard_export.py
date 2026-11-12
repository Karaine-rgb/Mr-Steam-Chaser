"""Scorecard and CSV export. The first tests are the predecessor tool's own
tally tests, copied unchanged (tally/result_side are copied code)."""
import csv
import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.api import create_app
from app.config import DEFAULT_BOOKMAKERS, load_settings
from app.export import EXPORT_COLUMNS, POLL_COLUMNS
from app.odds import MARGIN_COLUMNS
from app.scorecard import read_match, result_side, tally

BOOKS = list(DEFAULT_BOOKMAKERS)


KICKOFF = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)


def m(result, engine, steam):
    return {"result": result, "engine": engine, "steam_sides": steam}




def test_result_side():
    assert result_side(2, 1) == "home" and result_side(0, 1) == "away" and result_side(1, 1) == "draw"
    assert result_side(None, 1) is None and result_side(1, None) is None


def test_tally_counts_each_signal_and_the_overlap():
    rows = [
        m("home", "home", ["home"]),            # engine right, steam right, converged right
        m("away", "home", ["home", "draw"]),    # engine wrong, steam wrong (away not steaming), converged wrong
        m("draw", "home", ["draw"]),            # engine wrong (draw), steam right (draw steamed), not converged
        m("away", None, ["away", "home"]),      # pick'em: no engine pick; steam right (several sides, no magnitude used)
        m("home", "home", []),                  # engine right, no steam at all
        m("home", None, []),                    # nothing from either
    ]
    t = tally(rows)
    assert t["settled"] == 6
    assert t["engine"] == {"picks": 4, "right": 2, "no_pick": 2}
    assert t["steam"]["flagged"] == 4 and t["steam"]["right"] == 3 and t["steam"]["no_steam"] == 2
    assert t["steam"]["avg_sides"] == 1.5                      # (1 + 2 + 1 + 2) / 4
    assert t["steam"]["chance_right"] == 2.0                   # 6 covered sides / 3
    assert t["converged"] == {"count": 2, "right": 1}          # rows 1 and 2 only


def test_tally_empty_round():
    t = tally([])
    assert t["settled"] == 0 and t["steam"]["avg_sides"] is None and t["converged"] == {"count": 0, "right": 0}


def test_steam_right_when_the_winning_side_steamed_even_if_not_the_biggest():
    # winning side (away) is the smaller of two steaming sides: still counts, magnitude is not used
    assert tally([m("away", None, ["home", "away"])])["steam"]["right"] == 1


def test_read_match_uses_engine_and_confirmed_steam_only():
    a = {"engine": {"favorite": "home"},
         "steam": {"sides": [{"side": "home", "confirmed": True}, {"side": "away", "confirmed": False}]}}
    assert read_match(a) == {"engine": "home", "steam_sides": ["home"]}


def test_export_columns_are_fixed():
    # Changing this list changes the file shape for every past and future export:
    # update it only on purpose, together with the README.
    assert EXPORT_COLUMNS == [
        "round_label", "matchup_id", "event_id", "sport_key", "league_title", "home_team", "away_team",
        "kickoff", "home_score", "away_score", "snapshot_id", "captured_at", "market_last_update",
        "bookmaker", "market", "point", "home_price", "draw_price", "away_price",
        "fair_home", "fair_draw", "fair_away",
        "margin_total_pct", "margin_home_pp", "margin_draw_pp", "margin_away_pp",
        "margin_home_share_pct", "margin_draw_share_pct", "margin_away_share_pct",
    ]
    assert POLL_COLUMNS == ["round_label", "matchup_id", "event_id", "captured_at", "ledger_id", "bookmakers"]


@pytest.fixture
async def http(pool, client, clock):
    app = create_app(load_settings({"ODDS_API_MOCK": "1", "POLLER_ENABLED": "0"}), client=client, pool=pool)
    async with app.router.lifespan_context(app):
        st = app.state.app_state
        st.credits.clock = clock
        st.poller.clock = clock
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as h:
            yield h, st


async def add_round(h, label="R", n=3):
    ev = (await h.get("/api/discovery/events", params={"sports": "soccer_epl"})).json()["events"][:n]
    body = {"round_label": label, "bookmakers": BOOKS, "markets": ["h2h"], "preset": "balanced",
            "matches": [{"event_id": e["event_id"], "sport_key": e["sport_key"]} for e in ev]}
    assert (await h.post("/api/rounds/add", json=body)).status_code == 200


async def test_export_round_csv_and_download_record(http):
    h, st = http
    await add_round(h, "Week/1 \"x\"")
    await st.poller.tick()
    mid = (await h.get("/api/matches")).json()["matches"][0]["id"]
    await h.put(f"/api/matches/{mid}/result", json={"home_score": 1, "away_score": 1})
    r = await h.get("/api/export/round.csv", params={"round_label": 'Week/1 "x"'})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="sharp-odds-Week_1__x_.csv"' in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert list(rows[0].keys()) == EXPORT_COLUMNS
    assert {row["bookmaker"] for row in rows} >= {"pinnacle", "onexbet"}
    first = next(row for row in rows if int(row["matchup_id"]) == mid)
    assert first["home_score"] == "1" and first["away_score"] == "1"
    total = float(first["margin_total_pct"])
    parts = sum(float(first[c]) for c in ("margin_home_pp", "margin_draw_pp", "margin_away_pp"))
    assert parts == pytest.approx(total, abs=1e-4)
    # the download is recorded, so delete no longer needs the extra confirmation
    s = (await h.get("/api/storage")).json()["rounds"][0]
    assert s["downloaded_at"] and s["downloaded_snapshots"] == len(rows)
    polls = list(csv.DictReader(io.StringIO((await h.get("/api/export/round-polls.csv",
                                                         params={"round_label": 'Week/1 "x"'})).text)))
    assert len(polls) == 3 and "pinnacle" in polls[0]["bookmakers"].split("|")
    assert (await h.get("/api/export/round.csv", params={"round_label": "nope"})).status_code == 404


async def test_round_without_snapshots_still_exports_its_matches(http):
    h, _ = http
    await add_round(h, "Empty", n=2)
    rows = list(csv.DictReader(io.StringIO((await h.get("/api/export/round.csv",
                                                        params={"round_label": "Empty"})).text)))
    assert len(rows) == 2 and rows[0]["snapshot_id"] == "" and rows[0]["margin_total_pct"] == ""


async def test_scorecard_endpoint(http):
    h, st = http
    await add_round(h, "SC", n=3)
    await st.poller.tick()
    ms = (await h.get("/api/matches", params={"round_label": "SC"})).json()["matches"]
    assert all(m["engine"] in ("home", "away") for m in ms)
    for m, (a, b) in zip(ms, [(2, 0), (0, 0), (0, 3)]):
        await h.put(f"/api/matches/{m['id']}/result", json={"home_score": a, "away_score": b})
    sc = (await h.get("/api/scorecard", params={"round_label": "SC"})).json()
    assert sc["round"]["settled"] == 3 and sc["all"]["settled"] == 3
    assert sc["round"]["engine"]["picks"] == 3
    assert sc["round"]["steam"]["flagged"] == 0  # one snapshot each: no steam can exist
    assert sc["rounds"][0]["round_label"] == "SC"


async def test_match_detail_has_analysis(http):
    h, st = http
    await add_round(h, n=1)
    await st.poller.tick()
    mid = (await h.get("/api/matches")).json()["matches"][0]["id"]
    a = (await h.get(f"/api/matches/{mid}")).json()["analysis"]
    assert a["comparison"]["reference"]["source"] == "pinnacle"
    assert a["comparison"]["best"]["home"]["price"] > 1
    assert a["gaps"]["polls"] == 1 and a["steam"]["sides"] == []

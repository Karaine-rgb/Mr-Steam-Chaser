"""Dashboard endpoints: summary, detail, results, monitoring, refresh, storage, delete."""
from datetime import timedelta

import httpx
import pytest

from app.api import create_app
from app.config import DEFAULT_BOOKMAKERS, load_settings
from app.dashboard import reference_probs

BOOKS = list(DEFAULT_BOOKMAKERS)


def test_reference_prefers_pinnacle_then_consensus():
    pin = {"fair_home": 0.6, "fair_draw": 0.25, "fair_away": 0.15}
    other = {"fair_home": 0.5, "fair_draw": 0.3, "fair_away": 0.2}
    third = {"fair_home": 0.4, "fair_draw": 0.3, "fair_away": 0.3}
    assert reference_probs({"pinnacle": pin, "x": other})["source"] == "pinnacle"
    c = reference_probs({"x": other, "y": third, "z": pin})
    assert c["source"] == "consensus" and c["books"] == 3
    assert c["home"] + c["draw"] + c["away"] == pytest.approx(1.0)
    assert c["home"] == pytest.approx(0.5 / (0.5 + 0.3 + 0.2))
    assert reference_probs({}) is None


@pytest.fixture
async def http(pool, client, clock):
    app = create_app(load_settings({"ODDS_API_MOCK": "1", "POLLER_ENABLED": "0"}), client=client, pool=pool)
    async with app.router.lifespan_context(app):
        st = app.state.app_state
        st.credits.clock = clock
        st.poller.clock = clock
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as h:
            yield h, st


async def add_round(h, label="R", n=3, sport="soccer_epl"):
    ev = (await h.get("/api/discovery/events", params={"sports": sport})).json()["events"][:n]
    body = {"round_label": label, "bookmakers": BOOKS, "markets": ["h2h"], "preset": "balanced",
            "matches": [{"event_id": e["event_id"], "sport_key": e["sport_key"]} for e in ev]}
    assert (await h.post("/api/rounds/add", json=body)).status_code == 200
    return ev


async def test_summary_detail_after_a_poll(http):
    h, st = http
    await add_round(h)
    rows = (await h.get("/api/matches", params={"round_label": "R"})).json()["matches"]
    assert len(rows) == 3 and all(r["snapshots"] == 0 and r["reference"] is None for r in rows)
    await st.poller.tick()
    data = (await h.get("/api/matches", params={"round_label": "R"})).json()
    assert data["version"] >= 1
    r = data["matches"][0]
    assert r["snapshots"] > 0 and r["books"] >= 5 and r["reference"]["source"] == "pinnacle"
    assert r["next_snapshot"]["label"] == "24h"
    d = (await h.get(f"/api/matches/{r['id']}")).json()
    assert {s["bookmaker"] for s in d["snapshots"]} >= {"pinnacle", "onexbet"}
    assert d["polls"][0]["kind"] == "planned" and [p["label"] for p in d["plan"]][0] == "24h"
    assert (await h.get("/api/matches/999999")).status_code == 404


async def test_results_validation(http):
    h, _ = http
    await add_round(h, n=1)
    mid = (await h.get("/api/matches")).json()["matches"][0]["id"]
    assert (await h.put(f"/api/matches/{mid}/result", json={"home_score": 2, "away_score": 1})).status_code == 200
    assert (await h.get("/api/matches")).json()["matches"][0]["home_score"] == 2
    assert (await h.put(f"/api/matches/{mid}/result", json={"home_score": 2})).status_code == 400
    assert (await h.put(f"/api/matches/{mid}/result", json={"home_score": -1, "away_score": 0})).status_code == 400
    assert (await h.put(f"/api/matches/{mid}/result", json={})).status_code == 200  # clear both
    assert (await h.put("/api/matches/999999/result", json={"home_score": 1, "away_score": 1})).status_code == 404


async def test_monitor_toggle_and_no_restart_after_kickoff(http, clock):
    h, st = http
    ev = await add_round(h, n=1)
    mid = (await h.get("/api/matches")).json()["matches"][0]["id"]
    assert (await h.put(f"/api/matches/{mid}/monitor", params={"on": "false"})).status_code == 200
    assert (await h.put(f"/api/matches/{mid}/monitor", params={"on": "true"})).status_code == 200
    from app.parse import parse_time
    clock.now = parse_time(ev[0]["commence_time"]) + timedelta(minutes=1)
    assert (await h.put(f"/api/matches/{mid}/monitor", params={"on": "false"})).status_code == 200
    assert (await h.put(f"/api/matches/{mid}/monitor", params={"on": "true"})).status_code == 409


async def test_manual_refresh_flow(http):
    h, _ = http
    await add_round(h, "W", n=2)
    q = (await h.get("/api/rounds/refresh-quote", params={"round_label": "W"})).json()
    assert q["cost"] == 1 and q["leagues"][0]["matches"] == 2
    assert (await h.post("/api/rounds/refresh", json={"round_label": "W", "expected_cost": 3})).status_code == 409
    res = (await h.post("/api/rounds/refresh", json={"round_label": "W", "expected_cost": 1})).json()
    assert res["ok"] and res["results"][0]["status"] == "ok"
    credits = (await h.get("/api/credits")).json()
    assert credits["budget"]["used"] == 1 and credits["recent_paid_calls"][0]["kind"] == "manual"


async def test_delete_round_rules(http):
    h, st = http
    await add_round(h, "Old", n=2)
    r = await h.request("DELETE", "/api/rounds", params={"round_label": "Old"})
    assert r.status_code == 409 and "never_downloaded" in r.json()["detail"]
    r = await h.request("DELETE", "/api/rounds", params={"round_label": "Old", "allow_undownloaded": "true"})
    assert r.status_code == 409 and "tracked" in r.json()["detail"]
    for m in (await h.get("/api/matches")).json()["matches"]:
        await h.put(f"/api/matches/{m['id']}/monitor", params={"on": "false"})
    async with st.pool.acquire() as con:  # pretend it was downloaded (export arrives in phase 5)
        await con.execute("insert into round_downloads (round_label) values ('Old')")
    r = await h.request("DELETE", "/api/rounds", params={"round_label": "Old"})
    assert r.status_code == 200 and r.json()["deleted_matches"] == 2
    assert (await h.get("/api/matches")).json()["matches"] == []
    assert (await h.get("/api/rounds")).json() == []
    assert (await h.request("DELETE", "/api/rounds", params={"round_label": "Nope",
                                                            "allow_undownloaded": "true"})).status_code == 404


async def test_storage_endpoints(http):
    h, st = http
    await add_round(h, "S", n=2)
    await st.poller.tick()
    s = (await h.get("/api/storage")).json()
    assert s["usage"]["level"] == "ok" and s["rounds"][0]["round_label"] == "S"
    assert s["rounds"][0]["snapshots"] > 0 and s["rounds"][0]["downloaded_at"] is None
    assert (await h.get("/api/storage/status")).json()["limit_bytes"] == 500_000_000
    rec = (await h.post("/api/storage/reclaim")).json()
    assert rec["ok"] and "odds_snapshots" in rec["rewritten"]


async def test_poller_status(http):
    h, st = http
    await add_round(h, n=2)
    await st.poller.tick()
    p = (await h.get("/api/poller")).json()
    assert p["enabled"] is False and p["leader"] is True
    assert p["plan"]["next"]["labels"] == ["24h"] and p["plan"]["cost_this_month"] >= 4

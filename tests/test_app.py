"""HTTP endpoints of the phase-2 app, in mock mode on a test database."""
import httpx

from app.api import create_app
from app.config import load_settings


async def test_credit_endpoints(pool, client, mock_api):
    app = create_app(load_settings({"ODDS_API_MOCK": "1", "POLLER_ENABLED": "0"}), client=client, pool=pool)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            assert (await http.get("/api/health")).json() == {"ok": True, "mock": True}
            data = (await http.get("/api/credits")).json()
            assert data["mock"] is True and data["budget"]["used"] == 0
            assert data["budget"]["pause_line"] == 450
            mock_api.used = 12
            synced = (await http.post("/api/credits/sync")).json()
            assert synced["budget"]["used"] == 12
            est = (await http.post("/api/credits/estimate", json={"markets": ["h2h", "totals"]})).json()
            assert est == {"per_league_refresh": 2}
            assert "x-api-key" not in (await http.get("/api/credits")).text


async def test_optional_password_protects_everything_but_health(pool, client):
    app = create_app(load_settings({"ODDS_API_MOCK": "1", "POLLER_ENABLED": "0", "APP_PASSWORD": "pw-123"}),
                     client=client, pool=pool)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            assert (await http.get("/api/health")).status_code == 200
            r = await http.get("/api/credits")
            assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
            assert (await http.get("/", auth=("sharp", "wrong"))).status_code == 401
            assert (await http.get("/api/credits", auth=("other", "pw-123"))).status_code == 401
            assert (await http.get("/api/credits", auth=("sharp", "pw-123"))).status_code == 200
            assert (await http.post("/api/rounds/refresh", json={"round_label": "x", "expected_cost": 1})).status_code == 401
            bad = await http.get("/api/credits", headers={"authorization": "Basic !!!notbase64"})
            assert bad.status_code == 401


async def test_health_fails_when_database_is_gone(pool, client):
    app = create_app(load_settings({"ODDS_API_MOCK": "1", "POLLER_ENABLED": "0"}), client=client, pool=pool)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            await pool.close()
            assert (await http.get("/api/health")).status_code == 503

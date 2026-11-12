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

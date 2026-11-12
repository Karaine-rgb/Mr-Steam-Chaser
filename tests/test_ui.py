"""Runs the headless browser test (tests/ui/discovery_ui.cjs) against a real
app server in MOCK mode on a throwaway database. Skipped when Node.js or
Playwright are not installed, or when SKIP_UI=1."""
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import asyncpg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _playwright_available() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


SEED_ROUND = "Seeded steam demo"


async def seed_finished_round(url: str) -> None:
    """A finished round with known movement, written straight to the database:
    20 polls 30 min apart. Match 1: home steams in Pinnacle AND Betfair
    (confirmed), home wins. Match 2: away steams in 1xBet only, draw.
    Match 3: flat, away wins."""
    from datetime import datetime, timedelta, timezone

    from app.odds import fair_probs_from_decimal_odds

    con = await asyncpg.connect(url)
    from app import db as _db
    await con.execute(_db.SCHEMA_PATH.read_text())
    ko = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=1)
    times = [ko - timedelta(hours=10) + timedelta(minutes=30 * i) for i in range(20)]
    flat = [0.40] * 20
    steam_home = [0.40] * 17 + [0.42, 0.44, 0.45]
    away_up = [0.30] * 17 + [0.32, 0.34, 0.35]
    plans = [
        ("Seed Home FC", "Seed Away FC", 2, 0, {"pinnacle": ("home", steam_home), "betfair_ex_eu": ("home", steam_home),
                                                 "onexbet": ("home", flat)}),
        ("Seed City", "Seed Town", 1, 1, {"pinnacle": ("home", flat), "betfair_ex_eu": ("home", flat),
                                          "onexbet": ("away", away_up)}),
        ("Seed United", "Seed Rovers", 0, 1, {"pinnacle": ("home", flat), "betfair_ex_eu": ("home", flat),
                                              "onexbet": ("home", flat)}),
    ]
    await con.execute("insert into round_settings (round_label, bookmakers) values ($1, $2)",
                      SEED_ROUND, ["pinnacle", "betfair_ex_eu", "onexbet"])
    for i, (home, away, hs, as_, books) in enumerate(plans):
        mid = await con.fetchval(
            "insert into matchups (event_id, sport_key, league_title, home_team, away_team, commence_time, "
            "round_label, is_monitored, home_score, away_score, added_at) values ($1, 'soccer_epl', 'EPL', $2, $3, $4, "
            "$5, false, $6, $7, $8) returning id",
            f"{i:032d}", home, away, ko, SEED_ROUND, hs, as_, times[0])
        for t in times:
            await con.execute("insert into match_polls (matchup_id, captured_at, bookmakers) values ($1, $2, $3)",
                              mid, t, list(books))
        for book, (side, path) in books.items():
            last = None
            for t, v in zip(times, path):
                if v == last:
                    continue
                last = v
                draw = 0.27
                probs = {"home": v, "away": 1 - v - draw} if side == "home" else {"away": v, "home": 1 - v - draw}
                prices = [round(1 / (probs["home"] * 1.04), 3), round(1 / (draw * 1.04), 3), round(1 / (probs["away"] * 1.04), 3)]
                fair = fair_probs_from_decimal_odds(prices)
                await con.execute(
                    "insert into odds_snapshots (matchup_id, bookmaker, market, captured_at, home_price, draw_price, "
                    "away_price, fair_home, fair_draw, fair_away) values ($1, $2, 'h2h', $3, $4, $5, $6, $7, $8, $9)",
                    mid, book, t, *prices, *fair)
    await con.close()


@pytest.mark.skipif(os.environ.get("SKIP_UI") == "1", reason="SKIP_UI=1")
async def test_discovery_ui_all_widths(pg_server, tmp_path):
    if not _playwright_available():
        pytest.skip("Node.js with Playwright not available")
    name = "ui_" + uuid.uuid4().hex[:10]
    admin = await asyncpg.connect(pg_server)
    await admin.execute(f"create database {name}")
    await admin.close()
    await seed_finished_round(pg_server.replace("/postgres?", f"/{name}?"))
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env.update({"DATABASE_URL": pg_server.replace("/postgres?", f"/{name}?"), "ODDS_API_MOCK": "1",
                "ODDS_API_KEY": "", "POLL_TICK_SECONDS": "2"})
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--port", str(port)],
                              cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except OSError:
                time.sleep(0.25)
        else:
            pytest.fail("app server did not start:\n" + server.stdout.read1().decode(errors="replace"))
        run = subprocess.run(["node", str(ROOT / "tests" / "ui" / "discovery_ui.cjs")],
                             env={**env, "BASE_URL": f"http://127.0.0.1:{port}", "SHOT_DIR": str(tmp_path)},
                             capture_output=True, text=True, timeout=600)
        assert run.returncode == 0, run.stdout + run.stderr
        assert "UI OK" in run.stdout
    finally:
        server.terminate()
        server.wait(timeout=10)

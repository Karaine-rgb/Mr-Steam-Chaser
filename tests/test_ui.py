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

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _playwright_available() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


@pytest.mark.skipif(os.environ.get("SKIP_UI") == "1", reason="SKIP_UI=1")
async def test_discovery_ui_all_widths(pg_server, tmp_path):
    if not _playwright_available():
        pytest.skip("Node.js with Playwright not available")
    name = "ui_" + uuid.uuid4().hex[:10]
    admin = await asyncpg.connect(pg_server)
    await admin.execute(f"create database {name}")
    await admin.close()
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env.update({"DATABASE_URL": pg_server.replace("/postgres?", f"/{name}?"), "ODDS_API_MOCK": "1",
                "ODDS_API_KEY": ""})
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

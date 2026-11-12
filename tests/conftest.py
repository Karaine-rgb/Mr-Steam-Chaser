"""Shared fixtures.

Database tests run against a REAL throwaway Postgres 16 cluster started for
the test session (no Supabase, no network). If Postgres binaries are not
installed, those tests are skipped with a clear message.

No test may reach the real Odds API: the client refuses while pytest runs
(app.odds_api.RealApiForbidden), and every fixture here uses the mock.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import httpx
import pytest

from app import db
from app.credits import CreditManager
from app.mock_api import MockOddsApi
from app.odds_api import OddsApiClient

PG_BIN = Path("/usr/lib/postgresql/16/bin")


def _run_as_postgres(cmd: list[str]) -> list[str]:
    if os.geteuid() == 0:  # initdb refuses to run as root
        return ["runuser", "-u", "postgres", "--", *cmd]
    return cmd


@pytest.fixture(scope="session")
def pg_server():
    if not (PG_BIN / "initdb").exists():
        pytest.skip("Postgres 16 binaries not found; database tests skipped")
    base = Path(tempfile.mkdtemp(prefix="sharpodds-pg-"))
    data, sock = base / "data", base / "sock"
    sock.mkdir()
    if os.geteuid() == 0:
        shutil.chown(base, "postgres", "postgres")
        shutil.chown(sock, "postgres", "postgres")
    subprocess.run(_run_as_postgres([str(PG_BIN / "initdb"), "-D", str(data), "-A", "trust", "-U", "postgres"]),
                   check=True, capture_output=True)
    subprocess.run(_run_as_postgres([
        str(PG_BIN / "pg_ctl"), "-D", str(data), "-w", "-l", str(base / "log"),
        "-o", f"-k {sock} -c listen_addresses='' -c fsync=off", "start",
    ]), check=True, capture_output=True)
    try:
        yield f"postgresql://postgres@/postgres?host={sock}"
    finally:
        subprocess.run(_run_as_postgres([str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"]),
                       capture_output=True)
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
async def pool(pg_server):
    """A fresh, empty database with the schema applied, per test."""
    name = "t_" + uuid.uuid4().hex[:12]
    admin = await asyncpg.connect(pg_server)
    await admin.execute(f"create database {name}")
    await admin.close()
    url = pg_server.replace("/postgres?", f"/{name}?")
    p = await db.create_pool(url, statement_cache_size=0)
    await db.ensure_schema(p)
    try:
        yield p
    finally:
        await p.close()
        admin = await asyncpg.connect(pg_server)
        await admin.execute(f"drop database {name}")
        await admin.close()


class Clock:
    """Settable UTC clock."""

    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock(datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc))


@pytest.fixture
def mock_api(clock):
    return MockOddsApi(quota=500, clock=clock)


@pytest.fixture
async def client(mock_api):
    c = OddsApiClient("https://api.the-odds-api.com", "", transport=httpx.MockTransport(mock_api.handle), is_mock=True)
    yield c
    await c.aclose()


@pytest.fixture
def make_manager(pool, client, clock):
    def make(**kw) -> CreditManager:
        args = dict(limit=500, reserve=50, pause_pct=90.0, pending_stale_minutes=5.0, clock=clock)
        args.update(kw)
        return CreditManager(pool, client, **args)
    return make

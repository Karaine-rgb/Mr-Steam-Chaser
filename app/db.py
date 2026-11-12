"""asyncpg pool and idempotent schema setup."""
from __future__ import annotations

from pathlib import Path

import asyncpg

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

_pool: asyncpg.Pool | None = None


async def create_pool(database_url: str, statement_cache_size: int = 0, **kwargs) -> asyncpg.Pool:
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return await asyncpg.create_pool(
        database_url,
        min_size=1,
        max_size=kwargs.pop("max_size", 5),
        statement_cache_size=statement_cache_size,
        **kwargs,
    )


async def init_pool(database_url: str, statement_cache_size: int = 0) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await create_pool(database_url, statement_cache_size)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialised")
    return _pool


async def ensure_schema(pool: asyncpg.Pool) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as con:
        await con.execute(sql)

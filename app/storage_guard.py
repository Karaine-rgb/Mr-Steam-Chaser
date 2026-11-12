"""Database size guard for the Supabase free tier (500 MB, read-only above it).

Copied from the predecessor tool unchanged in logic: at DB_PAUSE_PCT of the
limit the poller stops writing new snapshots (and stops buying odds, since
they could not be stored), and only resumes once usage has dropped a couple
of points below that line, so it never flaps. Usage is Postgres'
pg_database_size, which includes dead rows until space is reclaimed.
"""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable


logger = logging.getLogger("storage_guard")

MB = 1_000_000
SIZE_QUERY = "select pg_database_size(current_database())"


def describe_usage(used_bytes: int, limit_bytes: int, warn_pct: float, critical_pct: float, pause_pct: float) -> dict:
    used_pct = used_bytes / limit_bytes * 100 if limit_bytes else 0.0
    if used_pct >= pause_pct:
        level = "paused"
    elif used_pct >= critical_pct:
        level = "critical"
    elif used_pct >= warn_pct:
        level = "warning"
    else:
        level = "ok"
    pause_bytes = int(limit_bytes * pause_pct / 100)
    return {
        "used_bytes": int(used_bytes),
        "limit_bytes": int(limit_bytes),
        "free_bytes": max(0, int(limit_bytes - used_bytes)),
        "used_pct": round(used_pct, 2),
        "level": level,
        "pause_bytes": pause_bytes,
        "usable_bytes": max(0, pause_bytes - int(used_bytes)),
    }


class StorageGuard:
    def __init__(
        self,
        limit_bytes: int,
        warn_pct: float,
        critical_pct: float,
        pause_pct: float,
        size_fn: Callable[[], Awaitable[int]],
        resume_margin_pct: float = 2.0,
        ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit_bytes = limit_bytes
        self.warn_pct = warn_pct
        self.critical_pct = critical_pct
        self.pause_pct = pause_pct
        self.resume_margin_pct = resume_margin_pct
        self._size_fn = size_fn
        self._ttl = ttl_seconds
        self._clock = clock
        self._cached: dict | None = None
        self._cached_at = 0.0
        self.paused = False

    async def usage(self, force: bool = False) -> dict:
        now = self._clock()
        if force or self._cached is None or now - self._cached_at >= self._ttl:
            used = int(await self._size_fn())
            self._cached = describe_usage(used, self.limit_bytes, self.warn_pct, self.critical_pct, self.pause_pct)
            self._cached_at = now
            if self.paused:
                if self._cached["used_pct"] < self.pause_pct - self.resume_margin_pct:
                    self.paused = False
            elif self._cached["used_pct"] >= self.pause_pct:
                self.paused = True
        return {**self._cached, "collection_paused": self.paused}

    async def collection_blocked(self) -> bool:
        """True while new data must not be written. Fails open if the size
        cannot be read - a database that cannot answer a size query is not
        accepting writes anyway, and a guard error must not stop collection."""
        try:
            await self.usage()
        except Exception:
            logger.warning("Could not read database size; not blocking collection", exc_info=True)
            return False
        return self.paused


def guard_for_pool(pool, settings) -> StorageGuard:
    async def size() -> int:
        async with pool.acquire() as con:
            return int(await con.fetchval(SIZE_QUERY))

    return StorageGuard(
        limit_bytes=int(settings.db_limit_mb * MB),
        warn_pct=settings.db_warn_pct,
        critical_pct=settings.db_critical_pct,
        pause_pct=settings.db_pause_pct,
        size_fn=size,
    )

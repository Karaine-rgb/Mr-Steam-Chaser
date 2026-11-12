"""Fast round deletion (adapted from the predecessor tool).

The request does the instant part only: the round's matches are relabelled
to a hidden marker, which removes the round from every list at once. The rows
are then purged in the background with set-based deletes, a few matches at a
time. A purge interrupted by a restart is resumed on the next startup,
because the hidden marker is still on the rows.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("round_delete")

DELETING_LABEL = "__deleting__"
CHUNK_MATCHES = 3
PARALLEL_CHUNKS = 3


class RoundDeleter:
    def __init__(self, pool) -> None:
        self.pool = pool
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    async def hide_round(self, round_label: str) -> int:
        """Instant step. Refuses (returns -1) while any match is still monitored."""
        async with self.pool.acquire() as con:
            async with con.transaction():
                rows = await con.fetch("select id, is_monitored from matchups where round_label = $1 for update",
                                       round_label)
                if not rows:
                    return 0
                if any(r["is_monitored"] for r in rows):
                    return -1
                await con.execute("update matchups set round_label = $1 where round_label = $2",
                                  DELETING_LABEL, round_label)
                await con.execute("delete from round_settings where round_label = $1", round_label)
                await con.execute("delete from round_downloads where round_label = $1", round_label)
        return len(rows)

    async def pending_count(self) -> int:
        async with self.pool.acquire() as con:
            return int(await con.fetchval("select count(*) from matchups where round_label = $1", DELETING_LABEL))

    async def _purge_chunk(self, ids: list[int]) -> None:
        async with self.pool.acquire() as con:
            await con.execute("delete from odds_snapshots where matchup_id = any($1::bigint[])", ids)
            await con.execute("delete from matchups where id = any($1::bigint[])", ids)

    async def purge_pending(self) -> int:
        removed = 0
        async with self._lock:
            while True:
                async with self.pool.acquire() as con:
                    ids = [r["id"] for r in await con.fetch(
                        "select id from matchups where round_label = $1", DELETING_LABEL)]
                if not ids:
                    return removed
                chunks = [ids[i:i + CHUNK_MATCHES] for i in range(0, len(ids), CHUNK_MATCHES)]
                sem = asyncio.Semaphore(PARALLEL_CHUNKS)

                async def run(chunk: list[int]) -> None:
                    async with sem:
                        await self._purge_chunk(chunk)

                await asyncio.gather(*(run(c) for c in chunks))
                removed += len(ids)

    async def _purge_safely(self) -> None:
        try:
            n = await self.purge_pending()
            if n:
                logger.info("Purged %d deleted matches", n)
        except Exception:
            logger.exception("Background round purge failed; it resumes on the next delete or restart")

    def start_purge(self) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(self._purge_safely())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

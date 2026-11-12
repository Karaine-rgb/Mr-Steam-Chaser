"""CSV export, one file per round.

EXPORT_COLUMNS is fixed: the shape of the file must not change between
exports (tests check the exact list). Rows are the STORED snapshots, which
are written only when a bookmaker's prices changed; to rebuild the price at
every poll, use the polls file (round_polls.csv) and carry the last row
forward. Margin columns use the predecessor tool's formulas.
"""
from __future__ import annotations

import csv
import io

from app.odds import MARGIN_COLUMNS, margin_columns

EXPORT_COLUMNS = [
    "round_label", "matchup_id", "event_id", "sport_key", "league_title", "home_team", "away_team",
    "kickoff", "home_score", "away_score", "snapshot_id", "captured_at", "market_last_update",
    "bookmaker", "market", "point", "home_price", "draw_price", "away_price",
    "fair_home", "fair_draw", "fair_away", *MARGIN_COLUMNS,
]
POLL_COLUMNS = ["round_label", "matchup_id", "event_id", "captured_at", "ledger_id", "bookmakers"]


def _fmt(v):
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


async def round_csv(pool, round_label: str) -> tuple[str, int, int]:
    """(csv text, snapshot rows, matches)."""
    async with pool.acquire() as con:
        rows = await con.fetch(
            "select m.round_label, m.id as matchup_id, m.event_id, m.sport_key, m.league_title, m.home_team, "
            "m.away_team, m.commence_time as kickoff, m.home_score, m.away_score, s.id as snapshot_id, "
            "s.captured_at, s.market_last_update, s.bookmaker, s.market, s.point, s.home_price, s.draw_price, "
            "s.away_price, s.fair_home, s.fair_draw, s.fair_away "
            "from matchups m left join odds_snapshots s on s.matchup_id = m.id "
            "where m.round_label = $1 order by m.commence_time, m.id, s.captured_at, s.bookmaker, s.market, s.point",
            round_label)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(EXPORT_COLUMNS)
    snaps = 0
    for r in rows:
        d = dict(r)
        if d["snapshot_id"] is not None:
            snaps += 1
            d.update(margin_columns(d["home_price"], d["draw_price"], d["away_price"],
                                    d["fair_home"], d["fair_draw"], d["fair_away"]))
        w.writerow([_fmt(d.get(c)) for c in EXPORT_COLUMNS])
    return buf.getvalue(), snaps, len({r["matchup_id"] for r in rows})


async def round_polls_csv(pool, round_label: str) -> str:
    async with pool.acquire() as con:
        rows = await con.fetch(
            "select m.round_label, m.id as matchup_id, m.event_id, p.captured_at, p.ledger_id, p.bookmakers "
            "from match_polls p join matchups m on m.id = p.matchup_id where m.round_label = $1 "
            "order by m.commence_time, m.id, p.captured_at", round_label)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(POLL_COLUMNS)
    for r in rows:
        d = dict(r)
        d["bookmakers"] = "|".join(d["bookmakers"])
        w.writerow([_fmt(d.get(c)) for c in POLL_COLUMNS])
    return buf.getvalue()


async def record_download(pool, round_label: str, snapshots: int) -> None:
    async with pool.acquire() as con:
        await con.execute(
            "insert into round_downloads (round_label, snapshot_count) values ($1, $2) "
            "on conflict (round_label) do update set downloaded_at = now(), "
            "snapshot_count = excluded.snapshot_count, download_count = round_downloads.download_count + 1",
            round_label, snapshots)

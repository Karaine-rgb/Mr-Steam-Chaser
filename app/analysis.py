"""Per-match analysis across bookmakers (1X2 / h2h only).

Definitions (shown on the dashboard as well):
  - Series per bookmaker: rebuilt at every poll time where the bookmaker was
    present (match_polls), carrying its last stored price forward, because
    only price CHANGES are stored.
  - Reference: Pinnacle when it priced the match; otherwise the CONSENSUS, the
    per-side median of every present book's fair probability at each poll
    time, renormalised to 100 %. The output always says which one.
  - Best price: the highest decimal price per outcome among the latest prices.
  - Off-market: a book's fair probability minus the reference's, per side, in
    percentage points. "Spread" is the range (max - min) across books.
  - Steam: steam_moves() from app/signals.py applied to each book's own
    series. A side's steam is CONFIRMED when at least two different books
    had a steam swing on that side, in the same direction, with overlapping
    time spans. Steam on coarse data only describes movement between
    snapshots; windows shorter than the gap between snapshots are skipped.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import median

from app.odds import margin_columns
from app.signals import CONFIG, favorite_strength, steam_moves

SIDES = ("home", "draw", "away")
REFERENCE_BOOK = "pinnacle"
CONFIRM_MIN_BOOKS = 2


def _as_signal_row(t: datetime, r: dict) -> dict:
    return {"captured_at": t, "fair_home_prob": r["fair_home"], "fair_draw_prob": r["fair_draw"],
            "fair_away_prob": r["fair_away"], "home_price": r["home_price"], "draw_price": r["draw_price"],
            "away_price": r["away_price"]}


def book_series(snapshots: list[dict], polls: list[dict]) -> dict[str, list[dict]]:
    """bookmaker -> chronological rows at every poll where it was present.

    snapshots: h2h rows {bookmaker, captured_at, prices, fair_*}
    polls: {captured_at, bookmakers[]}. Without polls (older data), the
    stored rows themselves are used.
    """
    stored: dict[str, list[dict]] = defaultdict(list)
    for s in sorted(snapshots, key=lambda x: x["captured_at"]):
        if s.get("fair_home") is not None:
            stored[s["bookmaker"]].append(s)
    if not polls:
        return {b: [_as_signal_row(r["captured_at"], r) for r in rows] for b, rows in stored.items()}
    out: dict[str, list[dict]] = defaultdict(list)
    for poll in sorted(polls, key=lambda p: p["captured_at"]):
        t = poll["captured_at"]
        for book in poll["bookmakers"]:
            rows = stored.get(book)
            if not rows:
                continue
            last = None
            for r in rows:  # latest stored row at or before this poll
                if r["captured_at"] <= t:
                    last = r
                else:
                    break
            if last is not None:
                out[book].append(_as_signal_row(t, last))
    return dict(out)


def consensus_series(series: dict[str, list[dict]]) -> list[dict]:
    by_time: dict[datetime, list[dict]] = defaultdict(list)
    for rows in series.values():
        for r in rows:
            by_time[r["captured_at"]].append(r)
    out = []
    for t in sorted(by_time):
        rows = by_time[t]
        med = {s: median(r[f"fair_{s}_prob"] for r in rows) for s in SIDES}
        total = sum(med.values()) or 1.0
        out.append({"captured_at": t, "books": len(rows), **{f"fair_{s}_prob": med[s] / total for s in SIDES}})
    return out


def reference_series(series: dict[str, list[dict]]) -> tuple[str, list[dict]]:
    if series.get(REFERENCE_BOOK):
        return REFERENCE_BOOK, series[REFERENCE_BOOK]
    return "consensus", consensus_series(series)


def comparison(series: dict[str, list[dict]]) -> dict:
    """Latest prices side by side, best price per outcome, off-market books."""
    latest = {b: rows[-1] for b, rows in series.items() if rows}
    if not latest:
        return {"books": [], "best": {}, "reference": None, "spread_pp": {}}
    source, ref_rows = reference_series(series)
    ref = ref_rows[-1]
    best = {}
    for s in SIDES:
        priced = [(r[f"{s}_price"], b) for b, r in latest.items() if r.get(f"{s}_price")]
        if priced:
            top = max(p for p, _ in priced)
            best[s] = {"price": top, "books": sorted(b for p, b in priced if p == top)}
    books = []
    for b, r in sorted(latest.items(), key=lambda kv: (kv[0] != REFERENCE_BOOK, kv[0])):
        off = {s: round((r[f"fair_{s}_prob"] - ref[f"fair_{s}_prob"]) * 100, 2) for s in SIDES}
        m = margin_columns(r["home_price"], r["draw_price"], r["away_price"],
                           r["fair_home_prob"], r["fair_draw_prob"], r["fair_away_prob"])
        books.append({
            "bookmaker": b, "captured_at": r["captured_at"].isoformat(),
            "prices": {s: r[f"{s}_price"] for s in SIDES},
            "fair": {s: r[f"fair_{s}_prob"] for s in SIDES},
            "margin_pct": m["margin_total_pct"],
            "off_market_pp": off, "off_market_max_pp": max(abs(v) for v in off.values()),
            "is_best": {s: b in best.get(s, {}).get("books", []) for s in SIDES},
        })
    spread = {s: round((max(r[f"fair_{s}_prob"] for r in latest.values())
                        - min(r[f"fair_{s}_prob"] for r in latest.values())) * 100, 2) for s in SIDES}
    return {"books": books, "best": best, "spread_pp": spread,
            "reference": {"source": source, "books": ref.get("books", 1),
                          **{s: ref[f"fair_{s}_prob"] for s in SIDES}}}


def _overlap(a: dict, b: dict) -> bool:
    return a["from"] <= b["to"] and b["from"] <= a["to"]


def steam_by_book(series: dict[str, list[dict]], kickoff: datetime | None) -> dict:
    per_book = {}
    for book, rows in series.items():
        moves = steam_moves(rows, kickoff)
        if moves:
            per_book[book] = moves
    sides = []
    for side in SIDES:
        swings = [(book, sw) for book, moves in per_book.items() for m in moves if m["side"] == side
                  for sw in m["swings"]]
        if not swings:
            continue
        confirmed_books = set()
        for i, (b1, s1) in enumerate(swings):
            for b2, s2 in swings[i + 1:]:
                same_dir = (s1["change_pp"] > 0) == (s2["change_pp"] > 0)
                if b1 != b2 and same_dir and _overlap(s1, s2):
                    confirmed_books.update({b1, b2})
        book_set = sorted({b for b, _ in swings})
        top_book, top = max(swings, key=lambda x: x[1]["magnitude_pp"])
        sides.append({
            "side": side, "books": book_set, "confirmed": len(confirmed_books) >= CONFIRM_MIN_BOOKS,
            "confirmed_books": sorted(confirmed_books), "biggest_pp": round(top["magnitude_pp"], 2),
            "biggest_book": top_book, "timing": top["timing"],
            "from": top["from"].isoformat(), "to": top["to"].isoformat(),
        })
    sides.sort(key=lambda x: (not x["confirmed"], -x["biggest_pp"]))
    return {"sides": sides, "per_book": {b: [{**m, "from": m["from"].isoformat(), "to": m["to"].isoformat(),
                                              "swings": [{**s, "from": s["from"].isoformat(), "to": s["to"].isoformat()}
                                                         for s in m["swings"]]}
                                             for m in moves] for b, moves in per_book.items()}}


def poll_gaps(polls: list[dict]) -> dict:
    times = sorted(p["captured_at"] for p in polls)
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
    return {"polls": len(times), "min_minutes": round(min(gaps)) if gaps else None,
            "median_minutes": round(median(gaps)) if gaps else None,
            "max_minutes": round(max(gaps)) if gaps else None,
            "steam_window_minutes": round(CONFIG["velocity_window_hours"] * 60)}


def analyse_match(snapshots: list[dict], polls: list[dict], kickoff: datetime | None) -> dict:
    h2h = [s for s in snapshots if s["market"] == "h2h"]
    series = book_series(h2h, polls)
    source, ref = reference_series(series)
    fav = favorite_strength(ref)
    return {
        "comparison": comparison(series),
        "steam": steam_by_book(series, kickoff),
        "engine": {"source": source, **fav},
        "gaps": poll_gaps(polls),
        # the rebuilt per-poll series, for charts: [time, fair H, D, A, price H, D, A]
        "series": {b: [[r["captured_at"].isoformat(), r["fair_home_prob"], r["fair_draw_prob"], r["fair_away_prob"],
                        r["home_price"], r["draw_price"], r["away_price"]] for r in rows]
                   for b, rows in series.items()},
    }

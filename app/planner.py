"""Cost estimator and poll planner (docs/plan.md sections 1 and 2.3).

Pure functions: no database, no network, no clock. Everything they need is
passed in, so they can be tested under any budget.

Cost (docs/odds_api_facts.md):
    one /odds call for one league = markets x ceil(bookmakers / 10)
One call covers every tracked match in that league, so the planner works per
LEAGUE: it collects the planned snapshot times of all tracked matches, merges
times that are close together into one call, and prices each call.

If the month's plan costs more than the credits left for automatic polling,
the planner drops planned snapshots in a fixed order until it fits:
  far-from-kickoff middle snapshots go first, then nearer ones, then the last
  one before kickoff, and the "when added" (opening) snapshot goes last.
Dropped snapshots are reported, never hidden.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

OPENING = "added"

# Offsets before kickoff, in minutes. "added" = a snapshot as soon as the match
# is tracked. Order here = the preset's own order, earliest first.
PRESETS: dict[str, tuple] = {
    "lean": (OPENING, 360, 60),
    "balanced": (OPENING, 1440, 360, 120, 30),
    "dense": (OPENING, 2880, 1440, 720, 360, 180, 60, 20),
}

MIN_TOLERANCE = timedelta(minutes=10)
TOLERANCE_FRACTION = 0.25  # a call up to 25 % of the offset early still counts


def estimate_cost(markets, bookmakers=None, regions=None) -> int:
    """Credits for one /odds call. bookmakers wins over regions, as in the API."""
    n_markets = len(set(markets))
    if n_markets == 0:
        return 0
    if bookmakers:
        return n_markets * math.ceil(len(set(bookmakers)) / 10)
    if regions:
        return n_markets * len(set(regions))
    raise ValueError("either bookmakers or regions is required")


def label_of(offset) -> str:
    if offset == OPENING:
        return OPENING
    return f"{offset // 60}h" if offset % 60 == 0 else f"{offset}m"


def tolerance(offset) -> timedelta:
    if offset == OPENING:
        return timedelta(0)
    return max(MIN_TOLERANCE, timedelta(minutes=offset * TOLERANCE_FRACTION))


def drop_rank(preset: str, offset) -> int:
    """0 = keep longest. opening 0, then nearer-to-kickoff offsets are kept longer."""
    if offset == OPENING:
        return 0
    numeric = sorted(o for o in PRESETS[preset] if o != OPENING)  # smallest offset first
    return 1 + numeric.index(offset)


@dataclass(frozen=True)
class TrackedMatch:
    event_id: str
    sport_key: str
    commence_time: datetime
    added_at: datetime
    preset: str = "balanced"
    markets: tuple = ("h2h",)
    bookmakers: tuple = ()


@dataclass(frozen=True)
class Point:
    event_id: str
    sport_key: str
    kickoff: datetime
    offset: object
    due: datetime
    rank: int

    @property
    def label(self) -> str:
        return label_of(self.offset)


@dataclass
class PlannedCall:
    sport_key: str
    time: datetime
    cost: int
    points: list[Point] = field(default_factory=list)

    @property
    def event_ids(self) -> list[str]:
        return sorted({p.event_id for p in self.points})

    @property
    def nearest_kickoff(self) -> datetime:
        return min(p.kickoff for p in self.points)

    @property
    def best_rank(self) -> int:
        return min(p.rank for p in self.points)


@dataclass
class Plan:
    calls: list[PlannedCall]          # all calls, this month and later
    dropped: list[Point]              # points removed to fit the budget
    cost_this_month: int
    available: int
    month_end: datetime
    dropped_calls: int = 0

    def calls_this_month(self) -> list[PlannedCall]:
        return [c for c in self.calls if c.time < self.month_end]


def league_cost(matches: list[TrackedMatch]) -> int:
    markets = set().union(*(m.markets for m in matches)) if matches else set()
    books = set().union(*(m.bookmakers for m in matches)) if matches else set()
    return estimate_cost(markets, books) if markets and books else 0


def pending_points(
    match: TrackedMatch, league_calls: list[datetime], now: datetime
) -> list[Point]:
    """Snapshot points of one match that still need a call."""
    if match.commence_time <= now:
        return []  # never poll at or after kickoff
    preset = PRESETS.get(match.preset, PRESETS["balanced"])
    out = []
    for offset in preset:
        if offset == OPENING:
            done = any(match.added_at <= c < match.commence_time for c in league_calls)
            due = match.added_at
        else:
            due = match.commence_time - timedelta(minutes=offset)
            if due < match.added_at:
                continue  # match was added after this point: nothing to catch up
            tol = tolerance(offset)
            done = any(due - tol <= c < match.commence_time for c in league_calls)
        if done:
            continue
        out.append(Point(match.event_id, match.sport_key, match.commence_time, offset,
                         max(due, now), drop_rank(match.preset, offset)))
    return out


def merge_points(points: list[Point], cost: int) -> list[PlannedCall]:
    """Greedy merge for one league: a call made a little early (within the
    point's tolerance) serves that point too."""
    calls: list[PlannedCall] = []
    for p in sorted(points, key=lambda x: (x.due, x.rank)):
        if calls:
            last = calls[-1]
            if p.due - tolerance(p.offset) <= last.time <= p.due and last.time < p.kickoff:
                last.points.append(p)
                continue
        calls.append(PlannedCall(p.sport_key, p.due, cost, [p]))
    return calls


def build_plan(
    matches: list[TrackedMatch],
    calls_by_league: dict[str, list[datetime]],
    now: datetime,
    month_end: datetime,
    available: int,
) -> Plan:
    by_league: dict[str, list[TrackedMatch]] = {}
    for m in matches:
        by_league.setdefault(m.sport_key, []).append(m)

    points: dict[str, list[Point]] = {}
    costs: dict[str, int] = {}
    for league, ms in by_league.items():
        costs[league] = league_cost(ms)
        pts = []
        for m in ms:
            pts.extend(pending_points(m, calls_by_league.get(league, []), now))
        points[league] = pts

    def assemble() -> list[PlannedCall]:
        calls = []
        for league, pts in points.items():
            calls.extend(merge_points(pts, costs[league]))
        return sorted(calls, key=lambda c: (c.time, c.nearest_kickoff))

    dropped: list[Point] = []
    dropped_calls = 0
    calls = assemble()
    budget = max(0, available)
    while sum(c.cost for c in calls if c.time < month_end) > budget:
        candidates = [c for c in calls if c.time < month_end]
        if not candidates:
            break
        # Drop a whole CALL (all matches it serves), the least important first:
        # highest best rank; among equals, the one furthest in the future.
        victim = max(candidates, key=lambda c: (c.best_rank, c.time))
        for p in victim.points:
            points[p.sport_key].remove(p)
            dropped.append(p)
        dropped_calls += 1
        calls = assemble()

    cost = sum(c.cost for c in calls if c.time < month_end)
    return Plan(calls, dropped, cost, budget, month_end, dropped_calls)


def next_due_call(plan: Plan, now: datetime, last_call_at: dict[str, datetime]) -> PlannedCall | None:
    """The call to make now, if any: nearest kickoff first, then the league
    whose last snapshot is oldest."""
    due = [c for c in plan.calls if c.time <= now and c.time < plan.month_end]
    if not due:
        return None
    oldest = datetime.min.replace(tzinfo=now.tzinfo)
    return min(due, key=lambda c: (c.nearest_kickoff, last_call_at.get(c.sport_key, oldest)))


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """UTC calendar month. The API resets "on the first of every month"; the
    time zone is not documented, so UTC is an assumption (docs/odds_api_facts.md)."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def selection_estimate(
    existing: list[TrackedMatch],
    new: list[TrackedMatch],
    calls_by_league: dict[str, list[datetime]],
    now: datetime,
    available: int,
) -> dict:
    """What adding `new` would cost: per refresh, this month, and per day.

    Uses an unlimited budget to price the full schedule, then compares with
    what is actually available, so the panel can warn before anything is spent.
    """
    _, month_end = month_bounds(now)
    huge = 10**9
    before = build_plan(existing, calls_by_league, now, month_end, huge)
    after = build_plan(existing + new, calls_by_league, now, month_end, huge)
    new_leagues = {m.sport_key for m in new}
    per_refresh = sum(league_cost([m for m in existing + new if m.sport_key == lg]) for lg in new_leagues)
    extra_month = after.cost_this_month - before.cost_this_month
    extra_later = sum(c.cost for c in after.calls) - sum(c.cost for c in before.calls) - extra_month
    days_left = max(1e-9, (month_end - now).total_seconds() / 86400)
    fitted = build_plan(existing + new, calls_by_league, now, month_end, available)
    return {
        "per_refresh": per_refresh,
        "this_month": extra_month,
        "next_month": extra_later,
        "plan_total_this_month": after.cost_this_month,
        "available": available,
        "fits": after.cost_this_month <= available,
        "dropped_calls_if_added": fitted.dropped_calls,
        "dropped_snapshots_if_added": len(fitted.dropped),
        "per_day_now": round(after.cost_this_month / days_left, 2),
        "days_of_budget_left": (round(available / (after.cost_this_month / days_left), 1)
                                if after.cost_this_month else None),
    }


def with_preset(match: TrackedMatch, preset: str) -> TrackedMatch:
    return replace(match, preset=preset)

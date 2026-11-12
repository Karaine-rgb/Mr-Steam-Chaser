"""Cost estimator and poll planner under different budgets."""
from datetime import datetime, timedelta, timezone

import pytest

from app.config import DEFAULT_BOOKMAKERS
from app.planner import (OPENING, TrackedMatch, build_plan, estimate_cost, month_bounds, next_due_call,
                         selection_estimate)

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
_, MONTH_END = month_bounds(NOW)
BOOKS = DEFAULT_BOOKMAKERS


def match(eid, league="soccer_epl", kickoff_h=72.0, added_h=0.0, preset="balanced", markets=("h2h",), books=BOOKS):
    return TrackedMatch(eid, league, NOW + timedelta(hours=kickoff_h), NOW + timedelta(hours=added_h),
                        preset, markets, tuple(books))


# ---- cost estimator --------------------------------------------------------

@pytest.mark.parametrize("markets,books,cost", [
    (["h2h"], BOOKS, 1),                      # the agreed 10 books
    (["h2h"], [f"b{i}" for i in range(11)], 2),
    (["h2h"], [f"b{i}" for i in range(20)], 2),
    (["h2h"], [f"b{i}" for i in range(21)], 3),
    (["h2h", "totals"], BOOKS, 2),
    (["h2h", "spreads", "totals"], BOOKS, 3),
    (["h2h", "h2h"], ["pinnacle", "pinnacle"], 1),  # duplicates do not count twice
])
def test_estimate_cost_bookmakers(markets, books, cost):
    assert estimate_cost(markets, books) == cost


def test_estimate_cost_regions_and_docs_examples():
    assert estimate_cost(["h2h"], regions=["us"]) == 1
    assert estimate_cost(["h2h", "spreads", "totals"], regions=["us"]) == 3
    assert estimate_cost(["h2h"], regions=["us", "uk", "eu"]) == 3
    assert estimate_cost(["h2h", "spreads", "totals"], regions=["us", "uk", "au"]) == 9
    assert estimate_cost([], BOOKS) == 0
    with pytest.raises(ValueError):
        estimate_cost(["h2h"])


# ---- schedule --------------------------------------------------------------

def test_balanced_single_match_has_five_calls():
    plan = build_plan([match("a")], {}, NOW, MONTH_END, 1000)
    assert [p.label for c in plan.calls for p in c.points] == [OPENING, "24h", "6h", "2h", "30m"]
    assert plan.cost_this_month == 5 and not plan.dropped
    assert plan.calls[0].time == NOW  # opening snapshot is due straight away


@pytest.mark.parametrize("preset,n", [("lean", 3), ("balanced", 5), ("dense", 8)])
def test_presets(preset, n):
    plan = build_plan([match("a", kickoff_h=100, preset=preset)], {}, NOW, MONTH_END, 1000)
    assert plan.cost_this_month == n


def test_same_league_same_kickoff_shares_calls():
    plan = build_plan([match("a"), match("b")], {}, NOW, MONTH_END, 1000)
    assert plan.cost_this_month == 5
    assert all(c.event_ids == ["a", "b"] for c in plan.calls)


def test_different_leagues_cost_separately():
    plan = build_plan([match("a"), match("b", league="soccer_germany_bundesliga")], {}, NOW, MONTH_END, 1000)
    assert plan.cost_this_month == 10


def test_close_kickoffs_merge_within_tolerance():
    # 15:00 and 17:00 the same day: the 24h points (tolerance 6h) merge, the 30m points do not.
    plan = build_plan([match("a", kickoff_h=72), match("b", kickoff_h=74)], {}, NOW, MONTH_END, 1000)
    labels = [sorted(p.label for p in c.points) for c in plan.calls]
    assert ["24h", "24h"] in labels
    assert plan.cost_this_month < 10


def test_league_cost_uses_union_of_rounds_books():
    many = [f"b{i}" for i in range(15)]  # 15 new + the 10 defaults = 25 books -> 3
    plan = build_plan([match("a"), match("b", books=many)], {}, NOW, MONTH_END, 1000)
    assert all(c.cost == 3 for c in plan.calls)


def test_history_satisfies_points():
    calls = {"soccer_epl": [NOW + timedelta(minutes=1)]}  # opening already taken
    plan = build_plan([match("a")], calls, NOW + timedelta(minutes=2), MONTH_END, 1000)
    assert OPENING not in [p.label for c in plan.calls for p in c.points]
    assert plan.cost_this_month == 4


def test_overdue_points_collapse_into_one_call_now():
    m = match("a", kickoff_h=72)
    later = NOW + timedelta(hours=70)  # 24h and 6h were missed, 2h is due right now
    plan = build_plan([m], {"soccer_epl": [NOW]}, later, MONTH_END, 1000)
    assert plan.calls[0].time == later
    assert sorted(p.label for p in plan.calls[0].points) == ["24h", "2h", "6h"]
    assert len(plan.calls) == 2  # that one catch-up call, then 30m


def test_points_before_added_time_are_skipped():
    plan = build_plan([match("a", kickoff_h=3, added_h=0)], {}, NOW, MONTH_END, 1000)
    assert [p.label for c in plan.calls for p in c.points] == [OPENING, "2h", "30m"]


def test_never_after_kickoff():
    plan = build_plan([match("a", kickoff_h=-1, added_h=-5)], {}, NOW, MONTH_END, 1000)
    assert plan.calls == []


def test_next_month_calls_not_counted():
    m = match("a", kickoff_h=(MONTH_END - NOW).total_seconds() / 3600 + 3)  # kickoff 3h into next month
    plan = build_plan([m], {}, NOW, MONTH_END, 1000)
    assert plan.cost_this_month == 3  # opening, 24h and 6h fall in this month
    assert sum(c.cost for c in plan.calls) == 5


# ---- thinning under budgets ------------------------------------------------

def test_huge_budget_drops_nothing():
    ms = [match(str(i), league=f"l{i}") for i in range(8)]
    plan = build_plan(ms, {}, NOW, MONTH_END, 10**6)
    assert plan.cost_this_month == 40 and not plan.dropped


def test_tight_budget_keeps_opening_and_last_first():
    plan = build_plan([match("a")], {}, NOW, MONTH_END, 3)
    kept = [p.label for c in plan.calls for p in c.points]
    assert kept == [OPENING, "2h", "30m"]
    assert sorted(p.label for p in plan.dropped) == ["24h", "6h"]
    assert plan.cost_this_month == 3


def test_budget_of_two_keeps_opening_and_last():
    plan = build_plan([match("a")], {}, NOW, MONTH_END, 2)
    assert [p.label for c in plan.calls for p in c.points] == [OPENING, "30m"]


def test_zero_budget_drops_everything():
    plan = build_plan([match("a"), match("b", league="x")], {}, NOW, MONTH_END, 0)
    assert plan.calls_this_month() == [] and len(plan.dropped) == 10 and plan.dropped_calls == 10


def test_shared_call_is_dropped_whole():
    plan = build_plan([match("a"), match("b")], {}, NOW, MONTH_END, 4)
    assert plan.cost_this_month == 4 and plan.dropped_calls == 1
    assert sorted(p.event_id for p in plan.dropped) == ["a", "b"]


def test_thinning_spreads_across_leagues_evenly_by_rank():
    ms = [match("a", league="l1"), match("b", league="l2")]
    plan = build_plan(ms, {}, NOW, MONTH_END, 6)  # needs 10
    dropped = sorted(p.label for p in plan.dropped)
    assert dropped == ["24h", "24h", "6h", "6h"]


def test_cost_never_exceeds_budget():
    ms = [match(str(i), league=f"l{i % 5}", kickoff_h=20 + i * 7) for i in range(17)]
    for budget in (0, 1, 5, 17, 40, 75, 450):
        plan = build_plan(ms, {}, NOW, MONTH_END, budget)
        assert plan.cost_this_month <= budget


# ---- choosing the next call ------------------------------------------------

def test_next_due_prefers_nearest_kickoff_then_oldest():
    ms = [match("a", league="l1", kickoff_h=48), match("b", league="l2", kickoff_h=5),
          match("c", league="l3", kickoff_h=5)]
    plan = build_plan(ms, {}, NOW, MONTH_END, 1000)
    pick = next_due_call(plan, NOW, {"l2": NOW - timedelta(hours=1), "l3": NOW - timedelta(hours=9)})
    assert pick.sport_key == "l3"


def test_next_due_none_when_nothing_due():
    plan = build_plan([match("a")], {"soccer_epl": [NOW]}, NOW, MONTH_END, 1000)
    assert next_due_call(plan, NOW, {}) is None


# ---- selection estimate (discovery panel) ----------------------------------

def test_selection_estimate_fits_and_warns():
    existing = [match("a", league="l1")]
    new = [match("b", league="l2"), match("c", league="l2")]
    est = selection_estimate(existing, new, {}, NOW, available=450)
    assert est["per_refresh"] == 1 and est["this_month"] == 5 and est["fits"]
    tight = selection_estimate(existing, new, {}, NOW, available=6)
    assert not tight["fits"] and tight["dropped_calls_if_added"] == 4
    assert tight["dropped_snapshots_if_added"] == 6  # l2's dropped calls each served 2 matches

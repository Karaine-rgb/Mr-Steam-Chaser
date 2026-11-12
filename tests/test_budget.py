"""Pure budget rules: lines, reserve, pause, soft cap (no database)."""
from datetime import datetime, timezone

import pytest

from app.credits import Budget, decide

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)  # 19 days left incl. today


def b(used=0, spent_today=0, limit=500, reserve=50, pause=90.0, exhausted=False, now=NOW, **kw):
    return Budget(limit=limit, reserve=reserve, pause_pct=pause, used=used, spent_today=spent_today,
                  now=now, exhausted=exhausted, **kw)


def test_default_lines_coincide_at_450():
    assert b().pause_line == 450 and b().auto_line == 450


def test_smaller_reserve_still_capped_by_pause():
    x = b(reserve=10)
    assert x.auto_line == 450 and x.pause_line == 450


def test_bigger_reserve_lowers_auto_line_only():
    x = b(reserve=100)
    assert x.auto_line == 400 and x.pause_line == 450


def test_auto_refused_past_auto_line_manual_allowed_to_pause_line():
    x = b(used=420, reserve=100)  # auto line 400
    assert decide(x, 1, "planned")[0] is False
    assert "reserve" in decide(x, 1, "planned")[1]
    assert decide(x, 1, "manual") == (True, "ok")
    assert decide(b(used=449, reserve=100), 2, "manual")[0] is False


def test_pause_at_threshold_blocks_everything():
    x = b(used=450)
    assert x.paused
    for kind in ("planned", "extra", "manual"):
        assert decide(x, 1, kind)[0] is False


def test_exactly_reaching_the_line_is_allowed():
    assert decide(b(used=449), 1, "planned") == (True, "ok")
    assert decide(b(used=449), 2, "planned")[0] is False


def test_exhausted_flag_blocks_even_below_line():
    x = b(used=10, exhausted=True)
    assert x.paused and decide(x, 1, "manual")[0] is False


def test_soft_cap_only_limits_extra_calls():
    x = b(used=0)
    assert x.days_left == 19
    assert x.soft_cap_today == 450 // 19  # 23
    full = b(used=23, spent_today=23)
    assert decide(full, 1, "extra")[0] is False
    assert decide(full, 1, "planned") == (True, "ok")


def test_soft_cap_uses_start_of_day_balance():
    x = b(used=100, spent_today=40)  # 60 used before today
    assert x.soft_cap_today == (450 - 60) // 19


def test_last_day_of_month():
    x = b(now=datetime(2026, 11, 30, 23, 0, tzinfo=timezone.utc))
    assert x.days_left == 1 and x.soft_cap_today == 450


def test_projection_and_quota_mismatch():
    x = b(used=100)
    assert x.projected_month_end == round(100 / ((NOW - NOW.replace(day=1, hour=0)).total_seconds() / 86400) * 30)
    assert b(api_used=10, api_remaining=490).api_quota_mismatch is False
    assert b(api_used=10, api_remaining=19990).api_quota_mismatch is True


@pytest.mark.parametrize("kind,cost", [("weird", 1), ("planned", 0)])
def test_bad_requests_refused(kind, cost):
    assert decide(b(), cost, kind)[0] is False

"""The predecessor tool's own tests for the copied steam functions, run
unchanged against app/signals.py to show the copy behaves the same on fine
data. (Extracted automatically; tests for functions not copied were left out.)"""
from datetime import datetime, timedelta, timezone

import pytest

from app.signals import CONFIG, _zigzag_pivot_indices, move_spans, steam_moves, velocity_shape


@pytest.fixture(autouse=True)
def old_window_rule(monkeypatch):
    """These tests describe the OLD behaviour, so the coarse-data rule is off
    here. tests/test_analysis.py checks the rule itself."""
    monkeypatch.setitem(CONFIG, "strict_windows", False)

KICKOFF = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)


def t(hours_before_kickoff):
    return KICKOFF - timedelta(hours=hours_before_kickoff)


def ml_point(hours_before, fh, fd, fa, limit=2500.0, status="open"):
    return {
        "captured_at": t(hours_before),
        "fair_home_prob": fh,
        "fair_draw_prob": fd,
        "fair_away_prob": fa,
        "limit_amount": limit,
        "status": status,
    }


def sp_point(hours_before, points, limit=5000.0, status="open"):
    return {
        "captured_at": t(hours_before),
        "home_points": points,
        "limit_amount": limit,
        "status": status,
    }


EARLY_AND_LATE = [
    ml_point(60, 0.40, 0.30, 0.30), ml_point(50, 0.40, 0.30, 0.30), ml_point(40, 0.40, 0.30, 0.30),
    ml_point(36, 0.40, 0.30, 0.30), ml_point(35, 0.43, 0.285, 0.285), ml_point(34, 0.47, 0.265, 0.265),
    ml_point(33.5, 0.45, 0.275, 0.275), ml_point(30, 0.43, 0.285, 0.285), ml_point(24, 0.41, 0.295, 0.295),
    ml_point(18, 0.40, 0.30, 0.30), ml_point(10, 0.40, 0.30, 0.30), ml_point(5, 0.40, 0.30, 0.30),
    ml_point(4, 0.40, 0.30, 0.30), ml_point(3.2, 0.44, 0.28, 0.28), ml_point(2.5, 0.48, 0.26, 0.26),
    ml_point(2, 0.46, 0.27, 0.27), ml_point(1, 0.44, 0.28, 0.28),
]



def test_velocity_shape_steam_when_move_concentrated_in_window():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(5, 0.40, 0.30, 0.30),   # flat right up until the window starts
        ml_point(0.5, 0.46, 0.28, 0.26),  # then the whole move happens inside it
    ]
    result = velocity_shape(series)
    assert result["label"] == "steam"
    assert result["side"] == "home"


def test_velocity_shape_reports_draw_as_the_side():
    # money moves onto the draw this time, not home or away - the old
    # version could never say this, since it only ever compared home vs
    # away and had no "side" output at all
    series = [
        ml_point(48, 0.34, 0.30, 0.36),
        ml_point(5, 0.34, 0.30, 0.36),
        ml_point(0.5, 0.31, 0.36, 0.33),
    ]
    result = velocity_shape(series)
    assert result["side"] == "draw"
    assert result["label"] == "steam"


def test_velocity_shape_drift_when_move_already_settled():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(40, 0.46, 0.28, 0.26),  # the move already happened, days ago
        ml_point(0.5, 0.46, 0.28, 0.26),  # nothing since - flat inside the window
    ]
    result = velocity_shape(series)
    assert result["label"] == "drift"


def test_velocity_shape_building_when_move_is_split():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(40, 0.43, 0.29, 0.28),  # half the move happens early
        ml_point(0.5, 0.46, 0.28, 0.26),  # the other half happens inside the window
    ]
    result = velocity_shape(series)
    assert result["label"] == "building"


def test_velocity_shape_reversal_when_recent_move_flips_direction():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(40, 0.50, 0.26, 0.24),  # moved home a lot, early
        ml_point(0.5, 0.44, 0.29, 0.27),  # recently reversed back toward away
    ]
    result = velocity_shape(series)
    assert result["label"] == "reversal"


def test_velocity_shape_quiet_below_floor():
    series = [ml_point(48, 0.400, 0.30, 0.300), ml_point(0.5, 0.401, 0.30, 0.299)]
    result = velocity_shape(series)
    assert result["label"] == "quiet"


def test_velocity_shape_insufficient_history_when_truly_too_recent():
    # only 18 minutes of total history - even the shrunk adaptive window
    # (see below) can't get above velocity_min_window_hours (15 min) here
    series = [ml_point(0.4, 0.40, 0.30, 0.30), ml_point(0.1, 0.46, 0.28, 0.26)]
    result = velocity_shape(series)
    assert result["label"] == "insufficient_history"


def test_velocity_shape_adaptive_window_still_classifies_a_short_history():
    # only 1.9h of total history - the old fixed-3h-window version would
    # have needed 6h and called this "insufficient_history" outright, even
    # though a real, fast move is sitting right there in the data
    series = [
        ml_point(2, 0.40, 0.30, 0.30),
        ml_point(1.9, 0.40, 0.30, 0.30),  # flat for almost the whole window
        ml_point(0.1, 0.46, 0.28, 0.26),  # then a real move near the end
    ]
    result = velocity_shape(series)
    assert result["label"] == "steam"
    assert result["window_hours"] < CONFIG["velocity_window_hours"]


def test_velocity_shape_swung_back_when_move_reverts_by_the_end():
    # a real 8pp swing happens, then it comes almost all the way back -
    # opening vs current alone would show ~0 change and call this "quiet"
    series = [
        ml_point(48, 0.400, 0.30, 0.300),
        ml_point(24, 0.480, 0.27, 0.250),  # swung hard toward home
        ml_point(0.5, 0.402, 0.30, 0.298),  # and back to near where it started
    ]
    result = velocity_shape(series)
    assert result["label"] == "swung_back"
    assert result["peak_change_pp"] == pytest.approx(8.0, abs=0.1)


def test_velocity_shape_no_label_with_fewer_than_two_snapshots():
    assert velocity_shape([])["label"] is None
    assert velocity_shape([ml_point(1, 0.4, 0.3, 0.3)])["label"] is None


def test_zigzag_pivot_indices_matches_the_swings():
    values = [0.40, 0.41, 0.46, 0.45, 0.43, 0.47]
    # up to 0.46 (index 2), down to 0.43 (index 4), up to 0.47 (index 5)
    assert _zigzag_pivot_indices(values) == [0, 2, 4, 5]
    assert _zigzag_pivot_indices([0.4]) == []


def test_move_spans_steam_runs_from_where_the_move_started_to_where_it_stopped():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(5, 0.40, 0.30, 0.30),
        ml_point(2, 0.43, 0.29, 0.28),
        ml_point(0.5, 0.46, 0.28, 0.26),  # +6pp on home inside the last 3 hours
    ]
    spans = move_spans(series)
    home = [g for g in spans if g["side"] == "home"]
    assert len(home) == 1
    assert home[0]["label"] == "steam"
    assert home[0]["from"] == t(5) and home[0]["to"] == t(0.5)
    assert home[0]["change_pp"] == pytest.approx(6.0)


def test_move_spans_ignores_swings_below_the_minimum_size():
    series = [ml_point(48, 0.400, 0.30, 0.300), ml_point(30, 0.404, 0.298, 0.298), ml_point(1, 0.401, 0.30, 0.299)]
    assert move_spans(series) == []


def test_move_spans_labels_the_pullback_as_a_reversal():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(40, 0.50, 0.26, 0.24),  # big move onto home, early
        ml_point(0.5, 0.45, 0.28, 0.27),  # recently backing off, still above the opening
    ]
    home = [g for g in move_spans(series) if g["side"] == "home"]
    assert [g["label"] for g in home][-1] == "reversal"
    assert home[-1]["change_pp"] == pytest.approx(-5.0)


def test_move_spans_skips_a_line_that_sits_below_its_opening_level():
    # away fell and stays down: money leaving, not a move onto away
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(5, 0.40, 0.30, 0.30),
        ml_point(0.5, 0.46, 0.28, 0.26),
    ]
    assert [g for g in move_spans(series) if g["side"] == "away"] == []


def test_move_spans_needs_a_few_snapshots():
    assert move_spans([ml_point(5, 0.4, 0.3, 0.3), ml_point(1, 0.5, 0.25, 0.25)]) == []


def test_velocity_shape_can_be_forced_onto_a_side():
    series = [ml_point(48, 0.40, 0.30, 0.30), ml_point(5, 0.40, 0.30, 0.30), ml_point(0.5, 0.46, 0.28, 0.26)]
    assert velocity_shape(series)["side"] == "home"
    assert velocity_shape(series, side="draw")["side"] == "draw"


def test_steam_moves_lists_each_side_with_a_steam_move_ranked_by_size():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(5, 0.40, 0.30, 0.30),
        ml_point(2, 0.43, 0.33, 0.24),
        ml_point(0.5, 0.46, 0.40, 0.14),  # home +6pp, draw +10pp, both inside the last 3 hours
    ]
    moves = steam_moves(series)
    assert [m["side"] for m in moves] == ["draw", "home"]
    assert moves[0]["magnitude_pp"] > moves[1]["magnitude_pp"]
    assert moves[0]["magnitude_pp"] == pytest.approx(10.0)
    assert moves[1]["magnitude_pp"] == pytest.approx(6.0)
    assert moves[0]["count"] == 1 and moves[0]["to"] == t(0.5)


def test_steam_moves_ignores_moves_that_were_not_steam():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(40, 0.46, 0.28, 0.26),  # the move happened days ago: drift, not steam
        ml_point(0.5, 0.46, 0.28, 0.26),
    ]
    assert steam_moves(series) == []


def test_steam_moves_empty_for_a_quiet_market():
    series = [ml_point(48, 0.400, 0.30, 0.300), ml_point(20, 0.401, 0.30, 0.299), ml_point(1, 0.400, 0.30, 0.300)]
    assert steam_moves(series) == []


def test_steam_moves_counts_repeated_steam_on_a_side_and_reports_the_biggest():
    series = [
        ml_point(48, 0.40, 0.30, 0.30),
        ml_point(30, 0.40, 0.30, 0.30),
        ml_point(29, 0.44, 0.29, 0.27),   # fast +4pp
        ml_point(20, 0.44, 0.29, 0.27),
        ml_point(19, 0.40, 0.31, 0.29),
        ml_point(5, 0.40, 0.31, 0.29),
        ml_point(4, 0.43, 0.30, 0.27),
        ml_point(2.5, 0.50, 0.26, 0.24),  # fast +10pp from 0.40
    ]
    moves = [m for m in steam_moves(series) if m["side"] == "home"]
    assert len(moves) == 1 and moves[0]["count"] >= 1
    assert moves[0]["magnitude_pp"] >= 4.0


def test_steam_moves_label_each_swing_early_or_late_and_show_where_the_magnitude_came_from():
    home = [m for m in steam_moves(EARLY_AND_LATE, KICKOFF) if m["side"] == "home"][0]
    assert home["magnitude_pp"] == pytest.approx(8.0)           # biggest swing = the late one
    assert home["timing"] == "late" and home["hours_before_kickoff"] == pytest.approx(2.5)
    assert home["early_pp"] == pytest.approx(7.0)               # the earlier swing, finished 34h out
    assert home["late_pp"] == pytest.approx(8.0)
    assert home["count"] == len(home["swings"]) == 3
    assert [s["timing"] for s in home["swings"]] == ["late", "early", "late"]
    assert [round(s["magnitude_pp"], 1) for s in home["swings"]] == [8.0, 7.0, 4.0]
    assert home["swings"][2]["change_pp"] == pytest.approx(-4.0)   # a falling steam swing keeps its sign


def test_steam_moves_without_kickoff_has_no_timing_but_the_same_ranking():
    plain = steam_moves(EARLY_AND_LATE)
    timed = steam_moves(EARLY_AND_LATE, KICKOFF)
    assert [(m["side"], round(m["magnitude_pp"], 6)) for m in plain] == [(m["side"], round(m["magnitude_pp"], 6)) for m in timed]
    assert all(m["timing"] is None and m["early_pp"] is None and m["late_pp"] is None for m in plain)


def test_late_means_finished_within_three_hours_of_kickoff():
    series = [ml_point(8, 0.40, 0.30, 0.30), ml_point(5, 0.40, 0.30, 0.30), ml_point(4, 0.43, 0.285, 0.285),
              ml_point(3, 0.50, 0.25, 0.25)]                  # swing finishes exactly 3h before kickoff
    on_line = [m for m in steam_moves(series, KICKOFF) if m["side"] == "home"][0]
    assert on_line["hours_before_kickoff"] == pytest.approx(3.0) and on_line["timing"] == "late"
    just_outside = [m for m in steam_moves(series, KICKOFF + timedelta(minutes=1)) if m["side"] == "home"][0]
    assert just_outside["timing"] == "early"


"""De-vig and margin columns (formulas copied unchanged from the predecessor tool)."""
import pytest

from app.odds import MARGIN_COLUMNS, fair_probs_from_decimal_odds, implied_prob, margin_columns, power_devig


def test_power_devig_sums_to_one_three_way():
    raw = [implied_prob(d) for d in (1.43, 5.18, 6.61)]  # live Pinnacle Man City v Fulham
    assert sum(raw) > 1.0
    fair = power_devig(raw)
    assert sum(fair) == pytest.approx(1.0, abs=1e-6)
    assert fair[0] > fair[1] > fair[2]


def test_power_devig_two_way_symmetric():
    fair = power_devig([implied_prob(1.91), implied_prob(1.91)])
    assert fair == pytest.approx([0.5, 0.5], abs=1e-6)


def test_power_devig_noop_when_fair():
    assert power_devig([0.5, 0.3, 0.2]) == pytest.approx([0.5, 0.3, 0.2], abs=1e-6)


def test_power_devig_handles_exchange_underround():
    # Exchange back prices before commission can sum to less than 100 %.
    prices = [2.1, 3.7, 4.2]
    assert sum(1 / p for p in prices) < 1.0
    fair = fair_probs_from_decimal_odds(prices)
    assert sum(fair) == pytest.approx(1.0, abs=1e-6)
    m = margin_columns(*prices, *fair)
    assert m["margin_total_pct"] < 0


def test_power_devig_rejects_bad_input():
    with pytest.raises(ValueError):
        power_devig([1.2, -0.1])
    with pytest.raises(ValueError):
        implied_prob(1.0)


def test_margin_columns_three_way_sums_and_shares():
    prices = [2.63, 3.68, 2.62]
    fair = fair_probs_from_decimal_odds(prices)
    m = margin_columns(*prices, *fair)
    total = (sum(1 / p for p in prices) - 1) * 100
    assert m["margin_total_pct"] == pytest.approx(total, abs=1e-5)
    assert m["margin_home_pp"] + m["margin_draw_pp"] + m["margin_away_pp"] == pytest.approx(total, abs=1e-4)
    shares = m["margin_home_share_pct"] + m["margin_draw_share_pct"] + m["margin_away_share_pct"]
    assert shares == pytest.approx(100.0, abs=1e-4)
    assert set(m) == set(MARGIN_COLUMNS)


def test_margin_columns_two_way_leaves_draw_empty():
    prices = [1.95, 1.90]
    fair = fair_probs_from_decimal_odds(prices)
    m = margin_columns(prices[0], None, prices[1], fair[0], None, fair[1])
    assert m["margin_draw_pp"] is None and m["margin_draw_share_pct"] is None
    assert m["margin_home_pp"] + m["margin_away_pp"] == pytest.approx(m["margin_total_pct"], abs=1e-4)


def test_margin_columns_missing_data_gives_all_none():
    assert all(v is None for v in margin_columns(2.0, None, None, 0.5, None, None).values())

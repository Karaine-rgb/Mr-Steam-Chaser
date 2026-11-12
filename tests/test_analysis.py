"""Analysis on hand-built series with known answers: series rebuild, reference,
comparison, steam on fine vs coarse data, confirmation across books, favourite."""
from datetime import datetime, timedelta, timezone

import pytest

from app.analysis import analyse_match, book_series, comparison, consensus_series, reference_series, steam_by_book
from app.signals import favorite_strength, velocity_shape

KO = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)


def snap(book, t, home, draw=0.27, margin=1.04):
    away = 1 - home - draw
    return {"bookmaker": book, "market": "h2h", "captured_at": t,
            "home_price": round(1 / (home * margin), 3), "draw_price": round(1 / (draw * margin), 3),
            "away_price": round(1 / (away * margin), 3), "fair_home": home, "fair_draw": draw, "fair_away": away}


def path(book, times, homes, **kw):
    """Store only changes, like the real ingest does."""
    out, last = [], None
    for t, h in zip(times, homes):
        if h != last:
            out.append(snap(book, t, h, **kw))
            last = h
    return out


def polls(times, books):
    return [{"captured_at": t, "bookmakers": list(books)} for t in times]


# fine data: every 30 min for 10 h; home flat at 40 %, then 40 -> 45 % in the last 1.5 h
FINE = [KO - timedelta(hours=10) + timedelta(minutes=30 * i) for i in range(20)]
FINE_HOME = [0.40] * 17 + [0.42, 0.44, 0.45]
# coarse data: balanced schedule times; the same 5 pp move happens between 6 h and 2 h
COARSE = [KO - timedelta(hours=h) for h in (48, 24, 6, 2, 0.5)]
COARSE_HOME = [0.40, 0.40, 0.40, 0.45, 0.45]


def test_series_rebuilt_at_every_poll_with_carry_forward():
    rows = path("pinnacle", FINE, FINE_HOME)
    assert len(rows) == 4  # only changes stored
    s = book_series(rows, polls(FINE, ["pinnacle"]))
    assert len(s["pinnacle"]) == 20 and s["pinnacle"][10]["fair_home_prob"] == 0.40


def test_missing_book_at_a_poll_is_not_invented():
    rows = path("x", FINE, FINE_HOME)
    p = polls(FINE, ["x"])
    p[5]["bookmakers"] = []  # x not listed in that poll
    assert len(book_series(rows, p)["x"]) == 19


def test_reference_pinnacle_else_consensus():
    a = book_series(path("pinnacle", FINE, FINE_HOME) + path("x", FINE, [0.5] * 20), polls(FINE, ["pinnacle", "x"]))
    assert reference_series(a)[0] == "pinnacle"
    b = book_series(path("x", FINE, [0.40] * 20) + path("y", FINE, [0.50] * 20) + path("z", FINE, [0.45] * 20),
                    polls(FINE, ["x", "y", "z"]))
    name, ref = reference_series(b)
    assert name == "consensus" and ref[0]["books"] == 3
    assert sum(ref[0][f"fair_{s}_prob"] for s in ("home", "draw", "away")) == pytest.approx(1.0)
    assert ref[0]["fair_home_prob"] == pytest.approx(0.45)


def test_comparison_best_price_and_off_market():
    t = FINE[:1]
    s = book_series([snap("pinnacle", t[0], 0.45, margin=1.02), snap("soft", t[0], 0.45, margin=1.08),
                     snap("odd", t[0], 0.40, margin=1.02)], polls(t, ["pinnacle", "soft", "odd"]))
    c = comparison(s)
    assert c["reference"]["source"] == "pinnacle"
    assert c["best"]["draw"]["books"] == ["odd", "pinnacle"]  # tie listed in full
    assert c["best"]["home"]["books"] == ["odd"]
    by = {b["bookmaker"]: b for b in c["books"]}
    assert by["pinnacle"]["off_market_max_pp"] == 0
    assert by["odd"]["off_market_pp"]["home"] == pytest.approx(-5.0)
    assert by["soft"]["margin_pct"] > by["pinnacle"]["margin_pct"]
    assert c["spread_pp"]["home"] == pytest.approx(5.0)
    assert [b["bookmaker"] for b in c["books"]][0] == "pinnacle"


def test_steam_found_on_fine_data():
    s = book_series(path("pinnacle", FINE, FINE_HOME), polls(FINE, ["pinnacle"]))
    st = steam_by_book(s, KO)
    (home,) = [x for x in st["sides"] if x["side"] == "home"]
    assert home["biggest_pp"] == pytest.approx(5.0) and home["timing"] == "late"
    assert home["confirmed"] is False  # one book only


def test_coarse_window_is_skipped_not_faked():
    rows = book_series(path("pinnacle", COARSE, COARSE_HOME), polls(COARSE, ["pinnacle"]))["pinnacle"]
    r = velocity_shape(rows[:4], side="home")  # judged at the 2 h snapshot: last gap 4 h > 3 h window
    assert r["label"] == "window_shorter_than_gap" and r["last_gap_hours"] == pytest.approx(4.0)
    # judged at the 30 min snapshot: last gap only 1.5 h, but the snapshot it measures
    # from is 6 h back, so "moved within 3 h" cannot be claimed either
    r2 = velocity_shape(rows, side="home")
    assert r2["label"] == "window_shorter_than_gap" and r2["measured_window_hours"] == pytest.approx(5.5)
    assert steam_by_book({"pinnacle": rows}, KO)["sides"] == []


def test_confirmed_needs_two_books_same_direction_overlapping():
    both = path("pinnacle", FINE, FINE_HOME) + path("betfair_ex_eu", FINE, FINE_HOME)
    st = steam_by_book(book_series(both, polls(FINE, ["pinnacle", "betfair_ex_eu"])), KO)
    home = next(x for x in st["sides"] if x["side"] == "home")
    assert home["confirmed"] and home["confirmed_books"] == ["betfair_ex_eu", "pinnacle"]

    # second book moves home the same way but 6 hours earlier: no overlap
    early = [0.40] * 5 + [0.42, 0.44, 0.45] + [0.45] * 12
    apart = path("pinnacle", FINE, FINE_HOME) + path("x", FINE, early)
    st2 = steam_by_book(book_series(apart, polls(FINE, ["pinnacle", "x"])), KO)
    assert not any(x["confirmed"] for x in st2["sides"])


def test_favorite_from_opening_1x2_and_lead_change():
    rows = book_series(path("pinnacle", FINE, FINE_HOME), polls(FINE, ["pinnacle"]))["pinnacle"]
    f = favorite_strength(rows)
    assert f["opening_favorite"] == "home" and f["favorite"] == "home" and not f["lead_change"]
    flip = book_series(path("p", FINE, [0.37] * 10 + [0.33] * 10), polls(FINE, ["p"]))["p"]  # away 36 -> 40 %
    g = favorite_strength(flip)
    assert g["opening_favorite"] == "home" and g["favorite"] == "away" and g["lead_change"]
    assert favorite_strength([])["favorite"] is None


def test_analyse_match_end_to_end():
    rows = path("pinnacle", FINE, FINE_HOME) + path("onexbet", FINE, FINE_HOME, margin=1.02)
    out = analyse_match(rows, polls(FINE, ["pinnacle", "onexbet"]), KO)
    assert out["engine"]["source"] == "pinnacle" and out["engine"]["favorite"] == "home"
    assert out["gaps"] == {"polls": 20, "min_minutes": 30, "median_minutes": 30, "max_minutes": 30,
                           "steam_window_minutes": 180}
    assert out["steam"]["sides"][0]["confirmed"]


def test_old_tools_own_steam_example_is_skipped_under_the_coarse_rule():
    """The predecessor's test called this steam: flat at 5 h, +6 pp at 0.5 h before
    kickoff. The move happened somewhere in a 4.5 h gap, so with the coarse-data rule
    on (the default) it is not called fast."""
    t = lambda h: KO - timedelta(hours=h)
    series = [{"captured_at": t(48), "fair_home_prob": 0.40, "fair_draw_prob": 0.30, "fair_away_prob": 0.30},
              {"captured_at": t(5), "fair_home_prob": 0.40, "fair_draw_prob": 0.30, "fair_away_prob": 0.30},
              {"captured_at": t(0.5), "fair_home_prob": 0.46, "fair_draw_prob": 0.28, "fair_away_prob": 0.26}]
    assert velocity_shape(series)["label"] == "window_shorter_than_gap"

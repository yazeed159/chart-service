"""Behavior tags (trade_tags.py): one scenario per tag."""
import os, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import trade_tags as tt  # noqa: E402

ET = ZoneInfo("America/New_York")
D = "2026-10-08"


def bars(start_hm, rows, ema=2.0, vwap=2.0):
    h, m = start_hm
    t0 = datetime(2026, 10, 8, h, m, tzinfo=ET)
    idx = [t0 + timedelta(minutes=i) for i in range(len(rows))]
    df = pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])
    df["Volume"] = 1000
    df["EMA9"] = ema
    df["VWAP"] = vwap
    return df


def trade(**kw):
    base = dict(id="T1", symbol="AAA", trade_date=D, entry_time="10:05:10", exit_time="10:09:30",
                entry_price=2.00, exit_price=2.02, shares=100, pnl_after_comm=2.0, side="long", fills=[])
    base.update(kw)
    return base


def test_sessions():
    cases = {"04:13:20": "session_early_premarket", "06:59:59": "session_early_premarket",
             "07:00:00": "session_pre_open", "09:29:59": "session_pre_open", "09:30:00": "session_open",
             "09:59:59": "session_open", "10:00:00": "session_midday", "14:59:00": "session_midday",
             "15:00:00": "session_power_hour", "16:00:00": "session_after_hours"}
    for t, want in cases.items():
        assert tt.compute_tags(trade(entry_time=t, exit_time=t))[0] == want, t


def test_scaled_in_out_and_added_to_loser():
    f = [
        dict(entry_time="10:05:10", entry_price=2.00, qty=100, entry_order="a", exit_time="10:09:00", exit_price=2.10, exit_order="x"),
        dict(entry_time="10:06:00", entry_price=1.95, qty=100, entry_order="b", exit_time="10:09:30", exit_price=2.12, exit_order="y"),
    ]
    got = tt.compute_tags(trade(fills=f))
    assert {"scaled_in", "scaled_out", "added_to_loser"} <= set(got)
    # adding HIGHER (to a winner) is scaled in but not "added to loser"
    f[1]["entry_price"] = 2.05
    got = tt.compute_tags(trade(fills=f))
    assert "scaled_in" in got and "added_to_loser" not in got
    # one order split over two fills is NOT scaling
    one = [dict(f[0], qty=50), dict(f[0], qty=50)]
    assert "scaled_in" not in tt.compute_tags(trade(fills=one))


def test_revenge_trade_same_symbol_within_5_min_of_a_loss():
    prev = dict(id="P", symbol="AAA", entry_time="10:00:00", exit_time="10:03:00", pnl_after_comm=-5.0, shares=100)
    me = trade(entry_time="10:06:30", exit_time="10:08:00")
    assert "revenge_trade" in tt.compute_tags(me, [prev, me])
    assert "revenge_trade" not in tt.compute_tags(trade(entry_time="10:09:30", exit_time="10:11:00"), [prev])  # 6.5 min later
    assert "revenge_trade" not in tt.compute_tags(me, [dict(prev, pnl_after_comm=3.0)])  # previous was a win
    assert "revenge_trade" not in tt.compute_tags(me, [dict(prev, symbol="BBB")])  # different symbol


def test_overtrading_from_the_10th_trade_and_repeat_symbol_from_the_3rd():
    day = [dict(id=f"X{i}", symbol="AAA" if i < 3 else "BBB", entry_time=f"10:{i:02d}:00", exit_time=f"10:{i:02d}:30",
                pnl_after_comm=1.0, shares=100) for i in range(12)]
    t9 = day[8]; t10 = day[9]
    assert "overtrading" not in tt.compute_tags(dict(t9, trade_date=D), day)
    assert "overtrading" in tt.compute_tags(dict(t10, trade_date=D), day)
    assert "repeat_symbol" in tt.compute_tags(dict(day[2], trade_date=D), day)      # 3rd AAA
    assert "repeat_symbol" not in tt.compute_tags(dict(day[1], trade_date=D), day)  # 2nd AAA


def test_oversized_needs_history_and_1_5x_median():
    assert "oversized" in tt.compute_tags(trade(shares=400), median_shares=200)
    assert "oversized" not in tt.compute_tags(trade(shares=250), median_shares=200)
    assert "oversized" not in tt.compute_tags(trade(shares=400), median_shares=None)


def test_entry_below_ema9_and_vwap():
    b = bars((10, 0), [(2.0, 2.05, 1.98, 2.02)] * 12, ema=2.10, vwap=2.10)
    got = tt.compute_tags(trade(entry_price=2.04), bars=b)
    assert "below_ema9" in got and "below_vwap" in got
    b2 = bars((10, 0), [(2.0, 2.05, 1.98, 2.02)] * 12, ema=1.90, vwap=1.90)
    got = tt.compute_tags(trade(entry_price=2.04), bars=b2)
    assert "below_ema9" not in got and "below_vwap" not in got


def test_gave_back_winner_winner_turned_loser_overstayed():
    # entry 10:05 @2.00; peaks at 2.10 (+5%) at 10:06; exits 10:12 @1.98 (a loss)
    rows = [(2.0, 2.0, 2.0, 2.0)] * 5 + [(2.00, 2.03, 1.99, 2.02), (2.02, 2.10, 2.01, 2.08)] + [(2.05, 2.06, 1.98, 1.99)] * 6
    b = bars((10, 0), rows)
    got = tt.compute_tags(trade(entry_time="10:05:10", exit_time="10:12:20", exit_price=1.98, pnl_after_comm=-3.0), bars=b)
    assert {"gave_back_winner", "winner_turned_loser", "overstayed"} <= set(got)


def test_small_peak_is_not_a_winner_and_quick_exit_is_not_overstaying():
    rows = [(2.0, 2.0, 2.0, 2.0)] * 5 + [(2.00, 2.02, 1.99, 2.01)] * 8   # peak +1% only
    got = tt.compute_tags(trade(exit_time="10:12:20", exit_price=1.98, pnl_after_comm=-3.0), bars=bars((10, 0), rows))
    assert not {"gave_back_winner", "winner_turned_loser", "overstayed"} & set(got)
    # peak +5% then sold 1 minute later near the peak: gave back <50%, not overstayed
    rows = [(2.0, 2.0, 2.0, 2.0)] * 5 + [(2.00, 2.10, 1.99, 2.08), (2.08, 2.09, 2.05, 2.07)]
    got = tt.compute_tags(trade(exit_time="10:06:40", exit_price=2.07, pnl_after_comm=6.0), bars=bars((10, 0), rows))
    assert not {"gave_back_winner", "winner_turned_loser", "overstayed"} & set(got)


def test_ran_after_exit_needs_10_percent_within_15_minutes():
    base = [(2.0, 2.0, 2.0, 2.0)] * 5 + [(2.00, 2.03, 1.99, 2.02)] * 5            # 10:00-10:09, exit at 10:09
    up = bars((10, 0), base + [(2.5, 2.8, 2.5, 2.7)])                              # 10:10 high 2.80
    got = tt.compute_tags(trade(exit_time="10:09:30", exit_price=2.50, pnl_after_comm=50.0), bars=up)
    assert "ran_after_exit" in got      # 2.5 -> 2.8 is +12%
    meh = bars((10, 0), base + [(2.5, 2.6, 2.5, 2.55)])                            # only +4%
    assert "ran_after_exit" not in tt.compute_tags(trade(exit_time="10:09:30", exit_price=2.50, pnl_after_comm=50.0), bars=meh)
    late = bars((10, 0), base + [(2.5, 2.5, 2.5, 2.5)] * 20 + [(2.8, 3.0, 2.8, 2.9)])   # runs 20+ min later
    assert "ran_after_exit" not in tt.compute_tags(trade(exit_time="10:09:30", exit_price=2.50, pnl_after_comm=50.0), bars=late)


def test_pipeline_style_keys_work():
    t = {"Symbol": "AAA", "Trade Date": D, "Entry Time": "04:13:20", "Exit Time": "04:14:27", "Entry Price": 1.72,
         "Exit Price": 1.67, "No. of Shares": 200, "P&L After Comm": -12.0, "Side": "Long", "Fills": []}
    assert tt.compute_tags(t)[0] == "session_early_premarket"


# ---------------------------------------------------------------------------
# Newer tags: hold time, price bracket, entry/exit shape, size & streaks, clean trade.
# ---------------------------------------------------------------------------
FLAT = (2.0, 2.05, 1.98, 2.02)


def test_hold_time_and_price_buckets():
    for exit_t, want in (("10:05:50", "hold_under_1m"), ("10:09:30", "hold_1_5m"),
                         ("10:20:00", "hold_5_30m"), ("10:40:00", "hold_30m_plus")):
        assert want in tt.compute_tags(trade(exit_time=exit_t))
    for px, want in ((1.99, "price_under_2"), (2.00, "price_2_5"), (5.00, "price_5_10"), (10.00, "price_10_plus")):
        assert want in tt.compute_tags(trade(entry_price=px))


def test_extended_entry_is_3_percent_over_the_ema9():
    b = bars((10, 0), [FLAT] * 12, ema=2.0)
    assert "extended_entry" in tt.compute_tags(trade(entry_price=2.07), bars=b)
    assert "extended_entry" not in tt.compute_tags(trade(entry_price=2.04), bars=b)


def test_entry_after_spike():
    rows = [(2.0, 2.0, 2.0, 2.0)] * 2 + [(2.00, 2.05, 2.00, 2.04), (2.04, 2.10, 2.04, 2.09), (2.09, 2.15, 2.08, 2.14)] + [FLAT] * 5
    assert "entry_after_spike" in tt.compute_tags(trade(entry_price=2.15), bars=bars((10, 0), rows))
    calm = [(2.0, 2.0, 2.0, 2.0)] * 2 + [(2.00, 2.02, 2.00, 2.01), (2.01, 2.03, 2.01, 2.02), (2.02, 2.04, 2.02, 2.03)] + [FLAT] * 5
    assert "entry_after_spike" not in tt.compute_tags(trade(entry_price=2.04), bars=bars((10, 0), calm))


def test_new_high_of_day_needs_bars_and_a_real_break():
    b = bars((10, 0), [FLAT] * 12)
    assert "new_high_of_day" in tt.compute_tags(trade(entry_price=2.06), bars=b)
    assert "new_high_of_day" not in tt.compute_tags(trade(entry_price=2.04), bars=b)
    short_day = bars((10, 3), [FLAT] * 12)      # only 2 bars before the 10:05 entry
    assert "new_high_of_day" not in tt.compute_tags(trade(entry_price=2.06), bars=short_day)


def test_premarket_high_break_is_the_first_break_only():
    pm = [(2.05, 2.10, 1.98, 2.05)] * 10        # 09:20-09:29, high 2.10
    reg = [(2.05, 2.08, 2.02, 2.06)] * 6        # 09:30-09:35
    t = trade(entry_time="09:36:10", exit_time="09:40:00", entry_price=2.11)
    assert "premarket_high_break" in tt.compute_tags(t, bars=bars((9, 20), pm + reg))
    already = reg[:2] + [(2.06, 2.12, 2.05, 2.10)] + reg[3:]    # already traded over 2.10 once
    assert "premarket_high_break" not in tt.compute_tags(t, bars=bars((9, 20), pm + already))
    assert "premarket_high_break" not in tt.compute_tags(trade(entry_time="09:36:10", exit_time="09:40:00", entry_price=2.05),
                                                         bars=bars((9, 20), pm + reg))


def test_vwap_reclaim():
    b = bars((10, 0), [FLAT] * 12)
    b["VWAP"] = [2.10] * 3 + [2.00] * 9          # closes (2.02) under VWAP, then back over it
    assert "vwap_reclaim" in tt.compute_tags(trade(entry_price=2.04), bars=b)
    assert "vwap_reclaim" not in tt.compute_tags(trade(entry_price=2.04), bars=bars((10, 0), [FLAT] * 12, vwap=2.0))


def test_sold_the_low_needs_a_bottom_of_bar_exit_and_a_bounce():
    base = [(2.0, 2.0, 2.0, 2.0)] * 8 + [(2.00, 2.01, 1.95, 1.96)]      # 10:08 exit bar, exit 1.96 near its low
    t = trade(entry_time="10:05:10", exit_time="10:08:30", exit_price=1.96, pnl_after_comm=-4.0)
    assert "sold_the_low" in tt.compute_tags(t, bars=bars((10, 0), base + [(1.97, 2.05, 1.97, 2.03)]))
    assert "sold_the_low" not in tt.compute_tags(t, bars=bars((10, 0), base + [(1.97, 1.99, 1.95, 1.97)]))


def test_sold_near_high_keeps_80_percent_of_the_peak_gain():
    rows = [(2.0, 2.0, 2.0, 2.0)] * 5 + [(2.00, 2.05, 2.00, 2.04), (2.04, 2.10, 2.03, 2.09), (2.09, 2.09, 2.07, 2.08)]
    b = bars((10, 0), rows)
    assert "sold_near_high" in tt.compute_tags(trade(exit_time="10:07:20", exit_price=2.09, pnl_after_comm=9.0), bars=b)
    assert "sold_near_high" not in tt.compute_tags(trade(exit_time="10:07:20", exit_price=2.05, pnl_after_comm=5.0), bars=b)


def test_deep_drawdown_is_3_percent_under_entry():
    rows = [(2.0, 2.0, 2.0, 2.0)] * 5 + [(2.00, 2.02, 1.93, 1.99)] + [(2.0, 2.03, 1.99, 2.02)] * 3
    t = trade(exit_time="10:08:20", exit_price=2.02, pnl_after_comm=2.0)
    assert "deep_drawdown" in tt.compute_tags(t, bars=bars((10, 0), rows))
    shallow = [(2.0, 2.0, 2.0, 2.0)] * 5 + [(2.00, 2.02, 1.97, 1.99)] + [(2.0, 2.03, 1.99, 2.02)] * 3
    assert "deep_drawdown" not in tt.compute_tags(t, bars=bars((10, 0), shallow))


def _d(i, sym, a, b, pnl, sh=100):
    return {"id": i, "symbol": sym, "entry_time": a, "exit_time": b, "pnl_after_comm": pnl, "shares": sh}


def test_sized_up_after_loss_losing_streak_and_rapid_fire():
    day = [_d("A", "BBB", "09:40:00", "09:41:00", -10), _d("B", "CCC", "09:45:00", "09:46:00", -5),
           _d("C", "DDD", "09:50:00", "09:52:00", -7)]
    me = trade(entry_time="09:52:30", exit_time="09:55:00", shares=200)
    got = tt.compute_tags(me, day_trades=day)
    assert {"sized_up_after_loss", "after_losing_streak", "rapid_fire"} <= set(got)
    calm = tt.compute_tags(trade(entry_time="10:30:00", exit_time="10:35:00", shares=100), day_trades=day)
    assert not {"sized_up_after_loss", "rapid_fire"} & set(calm)
    assert "after_losing_streak" in calm            # still the 4th trade after three straight losses
    broken = [day[0], _d("B", "CCC", "09:45:00", "09:46:00", +5), day[2]]
    assert "after_losing_streak" not in tt.compute_tags(trade(entry_time="10:30:00", exit_time="10:35:00"), day_trades=broken)


def test_clean_trade_is_a_planned_setup_with_no_process_problems():
    rows = [FLAT] * 4 + [(2.03, 2.04, 1.99, 2.00)] + [FLAT] * 7            # 10:04 is a red bar, high 2.04
    b = bars((10, 0), rows)
    ok = tt.compute_tags(trade(entry_price=2.05), bars=b)
    assert "clean_trade" in ok
    assert "clean_trade" not in tt.compute_tags(trade(entry_price=2.05), bars=b, median_shares=50)      # oversized
    assert "clean_trade" not in tt.compute_tags(trade(entry_price=2.20), bars=b)                         # a chase
    assert "clean_trade" not in tt.compute_tags(trade(entry_price=2.05))                                 # no bars to judge

"""Candle-by-candle checks for the three setup rules (setups.py)."""
import os, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import setups  # noqa: E402

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 8, 9, 40, tzinfo=ET)


def mk(rows, ema=None):
    """rows: list of (o, h, l, c) one per minute starting 09:40; ema: list or None."""
    idx = [T0 + timedelta(minutes=i) for i in range(len(rows))]
    df = pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])
    df["Volume"] = 1000
    df["EMA9"] = ema if ema is not None else df["Close"].ewm(span=9, adjust=False).mean()
    return df


def at(i, sec=20):
    return T0 + timedelta(minutes=i, seconds=sec)


FLAT = [(2.00, 2.02, 1.99, 2.01)] * 6


def test_red_to_green_entry_over_red_high():
    # bar 6 is red (2.05 -> 2.00, high 2.06); entry on bar 7 at 2.07 (over 2.06)
    rows = FLAT + [(2.05, 2.06, 1.99, 2.00), (2.01, 2.10, 2.00, 2.02)]
    bars = mk(rows)
    assert setups.classify(bars, at(7), 2.07) == setups.RED_TO_GREEN


def test_red_to_green_entry_bar_does_not_need_to_close_green():
    rows = FLAT + [(2.05, 2.06, 1.99, 2.00), (2.01, 2.10, 1.98, 1.99)]  # entry bar closes red
    assert setups.classify(mk(rows), at(7), 2.07) == setups.RED_TO_GREEN


def test_slippage_allowance_scales_with_price():
    # $5 stock: red bar high 5.06 -> 10c over (5.16) is well within 4% -> red-to-green; 24c over is a chase
    five = [(5.00, 5.02, 4.99, 5.01)] * 6 + [(5.04, 5.06, 4.98, 5.00), (5.01, 5.40, 5.00, 5.35)]
    assert setups.classify(mk(five), at(7), 5.16) == setups.RED_TO_GREEN
    assert setups.classify(mk(five), at(7), 5.30) == setups.CHASE
    # $2 stock: red bar high 2.06 -> up to 8c over (2.14) is fine, 9c over is a chase
    two = FLAT + [(2.05, 2.06, 1.99, 2.00), (2.01, 2.20, 2.00, 2.15)]
    assert setups.classify(mk(two), at(7), 2.14) == setups.RED_TO_GREEN
    assert setups.classify(mk(two), at(7), 2.15) == setups.CHASE
    assert setups.classify(mk(two), at(7), 2.08) == setups.RED_TO_GREEN


def test_chased_wick_break_is_tagged_chase():
    rows = FLAT + [(2.00, 2.10, 1.99, 2.03), (2.03, 2.25, 2.02, 2.20)]
    assert setups.classify(mk(rows), at(7), 2.20) == setups.CHASE  # 10c over a $2.10 high


def test_no_pattern_is_other_not_chase():
    # prior bar is a doji (neither red nor green): nothing to chase
    rows = FLAT + [(2.05, 2.06, 1.99, 2.05), (2.05, 2.30, 2.04, 2.25)]
    assert setups.classify(mk(rows), at(7), 2.25) == setups.OTHER


def test_entry_below_red_high_is_not_red_to_green():
    rows = FLAT + [(2.05, 2.06, 1.99, 2.00), (2.01, 2.10, 2.00, 2.02)]
    assert setups.classify(mk(rows), at(7), 2.03) != setups.RED_TO_GREEN


def test_green_over_green_wick():
    # prior bar green with a big upper wick (open 2.00 close 2.03 high 2.10), entry over 2.10
    rows = FLAT + [(2.00, 2.10, 1.99, 2.03), (2.03, 2.14, 2.02, 2.12)]
    assert setups.classify(mk(rows), at(7), 2.11) == setups.GREEN_OVER_GREEN_WICK


def test_green_without_real_wick_is_not_the_wick_setup():
    rows = FLAT + [(2.00, 2.031, 1.99, 2.03), (2.03, 2.14, 2.02, 2.12)]  # wick = 1 tenth of a cent
    assert setups.classify(mk(rows), at(7), 2.04) == setups.OTHER


def test_ema9_dip_buy():
    # rides above a rising EMA9, pulls back to touch it, entry bounces right at it
    up = [(2.00 + 0.03 * i, 2.04 + 0.03 * i, 1.99 + 0.03 * i, 2.03 + 0.03 * i) for i in range(10)]
    ema = [1.95 + 0.03 * i for i in range(10)]
    dip = [(2.28, 2.29, 2.19, 2.20), (2.20, 2.21, 2.16, 2.17), (2.17, 2.19, 2.15, 2.18)]
    ema += [2.17, 2.16, 2.16]
    rows = up + dip + [(2.18, 2.22, 2.17, 2.21)]
    ema += [2.16]
    bars = mk(rows, ema)
    # last completed bar (green, tiny wick, not over any red high) -> falls to the EMA rule
    assert setups.classify(bars, at(13), 2.19) == setups.EMA9_DIP_BUY


def test_red_to_green_beats_ema_dip_when_both_match():
    up = [(2.00 + 0.03 * i, 2.04 + 0.03 * i, 1.99 + 0.03 * i, 2.03 + 0.03 * i) for i in range(10)]
    ema = [1.95 + 0.03 * i for i in range(10)]
    dip = [(2.28, 2.29, 2.19, 2.20), (2.20, 2.21, 2.16, 2.17), (2.17, 2.18, 2.15, 2.16)]  # last bar red, high 2.18
    ema += [2.17, 2.16, 2.16]
    rows = up + dip + [(2.16, 2.23, 2.15, 2.22)]
    ema += [2.16]
    got = setups.matches(mk(rows, ema), at(13), 2.19)
    assert got[0] == setups.RED_TO_GREEN and setups.EMA9_DIP_BUY in got


def test_short_and_no_data_are_other():
    rows = FLAT + [(2.05, 2.06, 1.99, 2.00), (2.01, 2.10, 2.00, 2.02)]
    assert setups.classify(mk(rows), at(7), 2.07, "short") == setups.OTHER
    assert setups.classify(mk(rows[:0] or FLAT[:1]), at(0), 2.0) == setups.OTHER


def test_stale_prior_bar_does_not_count():
    # the red bar was 20 minutes before the entry bar (no trades in between)
    idx = [T0, T0 + timedelta(minutes=20)]
    df = pd.DataFrame([(2.05, 2.06, 1.99, 2.00), (2.01, 2.10, 2.00, 2.02)], index=idx,
                      columns=["Open", "High", "Low", "Close"])
    df["Volume"] = 1000
    df["EMA9"] = df["Close"]
    assert setups.classify(df, T0 + timedelta(minutes=20, seconds=20), 2.07) == setups.OTHER

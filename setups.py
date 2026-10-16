"""Rule-based setup tagging from the real 1-minute bars.

The trader's plan is long-only, entered on the 1-minute chart. Three named
setups, plus "other" when none of them is present:

  red_to_green           The last completed 1-min bar before the entry bar is RED
                         (close < open) and the entry fills above that red bar's
                         high, but not by more than the slippage allowance (4% of
                         the price: about 8c on a $2 stock, 20c on a $5 one). The
                         entry bar itself does not have to close green.

  green_over_green_wick  The last completed bar is GREEN but carries a real upper
                         wick, and the entry fills above that bar's high (i.e. over
                         the wick).

  ema9_dip_buy           A pullback to the 9 EMA: in the last few bars price dipped to
                         (or through) the 9 EMA after trading above it, and the entry
                         is a bounce that fills near the EMA, not far above it.

  chase                  The red-to-green / wick pattern was there, but the fill was
                         beyond the slippage allowance above the prior bar's high,
                         and no other setup fits. Tagged so a late entry is easy to
                         spot instead of hiding in "other".

If several match, the order above wins (chase only when nothing valid matched) (red_to_green is the primary plan). Shorts
are always "other" -- the plan is buying long. Nothing here guesses: it only looks
at candles, the entry price and the EMA9 already computed on the bars.

Works on the DataFrame the chart service already builds (index = naive ET minute
starts; columns Open/High/Low/Close/EMA9), so it adds no data fetches.
"""
from __future__ import annotations

from datetime import datetime, timedelta

RED_TO_GREEN = "red_to_green"
GREEN_OVER_GREEN_WICK = "green_over_green_wick"
EMA9_DIP_BUY = "ema9_dip_buy"
CHASE = "chase"
OTHER = "other"

SETUP_TYPES = (RED_TO_GREEN, GREEN_OVER_GREEN_WICK, EMA9_DIP_BUY, CHASE, OTHER)

# Human names, same strings the web UI shows (js/utils.js setupLabel).
SETUP_LABELS = {
    RED_TO_GREEN: "Red-to-green",
    GREEN_OVER_GREEN_WICK: "Green-over-green wick",
    EMA9_DIP_BUY: "9 EMA dip buy",
    CHASE: "Chase",
    OTHER: "Other",
}

# ---- tunables (all in one place so they're easy to adjust) -----------------
PRICE_EPS = 0.005          # fills are in cents: "above the high" allows half a cent of float noise
# Slippage allowance: a fill only counts as the break of the prior bar's high if it is within
# 4% of the price ABOVE that high (about 8c on a $2 stock, 20c on a $5 stock). Pure
# percentage so it scales with the stock; the 1c minimum only matters on sub-$0.25 names
# (fills are in whole cents). Further above than that the pattern was there but the entry
# was a chase.
MAX_ABOVE_TRIGGER_PCT = 0.04
MAX_ABOVE_TRIGGER_MIN = 0.01
MAX_GAP_MIN = 5            # prior bar must be within this many minutes (thin stocks skip empty minutes)
WICK_MIN_FRAC = 0.25       # upper wick must be >= 25% of the prior bar's full range...
WICK_MIN_ABS = 0.01        # ...and at least one cent
EMA_LOOKBACK_BARS = 3      # a "dip" = one of the last N bars before entry touched the EMA9
EMA_TOUCH_PCT = 0.004      # touch tolerance: within 0.4% (min 1c) above the EMA counts as a touch
EMA_ENTRY_MAX_ABOVE_PCT = 0.015   # entry must fill within 1.5% above the EMA9
EMA_ENTRY_MAX_BELOW_PCT = 0.010   # ...and not more than 1% below it
EMA_TREND_BARS = 8         # bars before the dip window checked for "was trading above the EMA"
EMA_TREND_MIN_ABOVE = 3    # at least this many of them closed above the EMA9


def _minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _rows_before_entry(bars, entry_dt: datetime):
    """(prior_rows, entry_row_or_None): completed bars strictly before the entry
    minute, oldest first, and the bar covering the entry minute if it exists."""
    entry_min = _minute(entry_dt)
    entry_row = None
    if entry_min in bars.index:
        entry_row = bars.loc[entry_min]
        if getattr(entry_row, "ndim", 1) > 1:  # duplicate index guard
            entry_row = entry_row.iloc[-1]
    prior = bars.loc[bars.index < entry_min]
    return prior, entry_row


def _is_red(r) -> bool:
    return float(r["Close"]) < float(r["Open"])


def _is_green(r) -> bool:
    return float(r["Close"]) > float(r["Open"])


def _upper_wick(r) -> float:
    return float(r["High"]) - max(float(r["Open"]), float(r["Close"]))


def matches(bars, entry_dt: datetime, entry_price: float, side: str = "long") -> list[str]:
    """Every setup whose rules are met, in priority order (may be empty)."""
    if str(side or "long").lower() == "short":
        return []
    if bars is None or len(bars) == 0 or entry_price is None:
        return []
    prior, entry_row = _rows_before_entry(bars, entry_dt)
    if len(prior) == 0:
        return []

    out: list[str] = []
    prev = prior.iloc[-1]
    gap = _minute(entry_dt) - prior.index[-1]
    adjacent = gap <= timedelta(minutes=MAX_GAP_MIN)
    px = float(entry_price)
    prev_high = float(prev["High"])
    allowance = max(prev_high * MAX_ABOVE_TRIGGER_PCT, MAX_ABOVE_TRIGGER_MIN)
    over_high = px >= prev_high - PRICE_EPS
    within_allowance = px <= prev_high + allowance + PRICE_EPS

    pattern = None
    if adjacent and over_high:
        if _is_red(prev):
            pattern = RED_TO_GREEN
        elif _is_green(prev):
            rng = prev_high - float(prev["Low"])
            wick = _upper_wick(prev)
            if wick >= WICK_MIN_ABS and rng > 0 and wick >= WICK_MIN_FRAC * rng:
                pattern = GREEN_OVER_GREEN_WICK
    if pattern and within_allowance:
        out.append(pattern)

    if _ema9_dip(prior, entry_row, px):
        out.append(EMA9_DIP_BUY)
    if not out and pattern:  # right pattern, entered too far above the trigger
        out.append(CHASE)
    return out


def _ema9_dip(prior, entry_row, px: float) -> bool:
    if "EMA9" not in prior.columns or len(prior) < EMA_LOOKBACK_BARS + 2:
        return False
    # EMA9 as known at the last completed bar (no look-ahead into the entry bar).
    ema = float(prior.iloc[-1]["EMA9"])
    if not ema > 0:
        return False
    window = prior.tail(EMA_LOOKBACK_BARS)
    lows = [float(x) for x in window["Low"]]
    if entry_row is not None:
        lows.append(float(entry_row["Low"]))
    tol = max(ema * EMA_TOUCH_PCT, 0.01)
    touched = min(lows) <= ema + tol
    if not touched:
        return False
    if not (ema * (1 - EMA_ENTRY_MAX_BELOW_PCT) <= px <= ema * (1 + EMA_ENTRY_MAX_ABOVE_PCT)):
        return False
    # It was a *pullback*: before the dip window price was riding above the EMA9.
    before = prior.iloc[:-EMA_LOOKBACK_BARS].tail(EMA_TREND_BARS)
    if len(before) == 0:
        return False
    above = int((before["Close"] > before["EMA9"]).sum())
    return above >= min(EMA_TREND_MIN_ABOVE, len(before))


def classify(bars, entry_dt: datetime, entry_price: float, side: str = "long") -> str:
    """The single tag for a trade: first matching setup, else 'other'."""
    m = matches(bars, entry_dt, entry_price, side)
    return m[0] if m else OTHER

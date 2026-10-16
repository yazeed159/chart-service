"""Automatic behavior tags, computed from the fills, the day's other trades and the
real 1-minute bars. Long side only (the trader never shorts); nothing here guesses.

Session (when you entered, ET)
  session_early_premarket  04:00-07:00        session_pre_open     07:00-09:30
  session_open             09:30-10:00        session_midday       10:00-15:00
  session_power_hour       15:00-16:00        session_after_hours  16:00+

From the fills
  scaled_in        more than one entry order            scaled_out   more than one exit order
  added_to_loser   a later buy filled below the average cost of the earlier buys

From the day's trades
  revenge_trade    re-entered the SAME symbol within 5 min of a losing trade in it
  overtrading      the 10th trade (or later) of the day
  repeat_symbol    the 3rd (or later) trade in the same symbol that day
  oversized        shares > 1.5x the median size of all your trades

From the bars (VWAP / EMA9 read at the last completed bar before entry)
  below_vwap       entry filled under VWAP          below_ema9   entry filled under the 9 EMA
  gave_back_winner     the trade peaked >= 2% over entry, then you gave back >= 50% of that peak gain
  winner_turned_loser  it peaked >= 2% over entry, and you still closed it at a loss
  overstayed       you stayed >= 5 min past the peak, having given back >= 25% of the peak gain
  ran_after_exit   within 15 min of exiting, price traded >= 10% above your exit

Entry shape, from the bars (read at the last completed bar before entry)
  extended_entry       entry filled >= 3% above the 9 EMA
  entry_after_spike    the 3 bars before entry ran up >= 6% (first open to last close)
  new_high_of_day      entry at/over the highest high of the day so far (needs 5+ bars of the day)
  premarket_high_break entered after 09:30 at/over the premarket high, the first time it was taken out
  vwap_reclaim         within the last 10 bars price closed under VWAP, the last bar closed back over it

Exit shape, from the bars
  sold_the_low         exit in the bottom 20% of its 1-min bar, then price traded >= 3% above your exit within 5 min
  sold_near_high       the trade peaked >= 2% over entry and you exited having kept >= 80% of the peak gain
  deep_drawdown        price traded >= 3% below your entry while you were in it

Size and streaks, from the day's trades
  sized_up_after_loss  the previous trade of the day lost and this one is >= 1.5x its size
  after_losing_streak  the 3 trades right before this one (same day) all lost
  rapid_fire           re-entered within 60 s of your last exit (any symbol)

Always present
  hold_under_1m / hold_1_5m / hold_5_30m / hold_30m_plus   entry-to-exit time
  price_under_2 / price_2_5 / price_5_10 / price_10_plus   entry price
  clean_trade          a valid setup (red-to-green, green-over-green wick, 9 EMA dip) AND none of:
                       added_to_loser, revenge_trade, overtrading, oversized, extended_entry,
                       sized_up_after_loss, after_losing_streak. Process, not outcome.

All thresholds are constants below.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

TAG_LABELS = {
    "clean_trade": "Clean trade",
    "session_early_premarket": "Early premarket (4–7)",
    "session_pre_open": "Pre-open (7–9:30)",
    "session_open": "Open (9:30–10)",
    "session_midday": "Midday (10–15)",
    "session_power_hour": "Power hour (15–16)",
    "session_after_hours": "After hours",
    "scaled_in": "Scaled in",
    "scaled_out": "Scaled out",
    "added_to_loser": "Added to loser",
    "revenge_trade": "Revenge trade",
    "overtrading": "Overtrading",
    "repeat_symbol": "Repeat symbol",
    "oversized": "Oversized",
    "below_vwap": "Entry below VWAP",
    "below_ema9": "Entry below 9 EMA",
    "gave_back_winner": "Gave back winner",
    "winner_turned_loser": "Winner turned loser",
    "overstayed": "Overstayed",
    "ran_after_exit": "Ran after exit",
    "extended_entry": "Extended entry",
    "entry_after_spike": "Entry after spike",
    "new_high_of_day": "New high of day",
    "premarket_high_break": "Premarket high break",
    "vwap_reclaim": "VWAP reclaim",
    "sold_the_low": "Sold the low",
    "sold_near_high": "Sold near the high",
    "deep_drawdown": "Deep drawdown",
    "sized_up_after_loss": "Sized up after loss",
    "after_losing_streak": "After 3 losses in a row",
    "rapid_fire": "Rapid-fire re-entry",
    "hold_under_1m": "Held < 1 min",
    "hold_1_5m": "Held 1–5 min",
    "hold_5_30m": "Held 5–30 min",
    "hold_30m_plus": "Held 30+ min",
    "price_under_2": "Price under $2",
    "price_2_5": "Price $2–5",
    "price_5_10": "Price $5–10",
    "price_10_plus": "Price $10+",
}

# Tags that stop a trade being "clean" (process problems only, never outcome tags).
NOT_CLEAN = frozenset({"added_to_loser", "revenge_trade", "overtrading", "oversized", "extended_entry",
                       "sized_up_after_loss", "after_losing_streak"})
ALL_TAGS = tuple(TAG_LABELS)

# ---- tunables ---------------------------------------------------------------
PRICE_EPS = 0.005
REVENGE_WINDOW_MIN = 5
OVERTRADING_FROM = 10          # the 10th trade of the day and every one after it
REPEAT_SYMBOL_FROM = 3         # the 3rd trade in the same symbol that day and after
OVERSIZED_MULT = 1.5
MEDIAN_MIN_TRADES = 10         # don't judge size until there is some history
PEAK_MIN_PCT = 0.02            # a peak must be >= 2% over entry to count as "was winning"
GAVE_BACK_FRAC = 0.50
OVERSTAY_MIN_AFTER_PEAK = 5
OVERSTAY_GIVEBACK_FRAC = 0.25
RAN_AFTER_EXIT_PCT = 0.10
RAN_AFTER_EXIT_WINDOW_MIN = 15
EXTENDED_ABOVE_EMA_PCT = 0.03  # entry >= 3% above the 9 EMA
SPIKE_BARS = 3                 # look at the last 3 completed bars...
SPIKE_PCT = 0.06               # ...that ran up >= 6%
NHOD_MIN_BARS = 5              # need this many bars of the day before "new high of day" means anything
VWAP_RECLAIM_LOOKBACK = 10     # a close under VWAP within this many bars before the last one
SOLD_LOW_BAR_FRAC = 0.20       # exit in the bottom 20% of its bar's range
SOLD_LOW_RECOVER_PCT = 0.03    # then price traded >= 3% above the exit...
SOLD_LOW_WINDOW_MIN = 5        # ...within this many minutes
NEAR_HIGH_KEEP_FRAC = 0.80     # kept >= 80% of the peak gain
DRAWDOWN_PCT = 0.03            # price traded >= 3% under entry while in the trade
SIZE_UP_MULT = 1.5
LOSING_STREAK_LEN = 3
RAPID_FIRE_SEC = 60


def _g(d: dict, *keys, default=None):
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return default


def _parse_t(s):
    if not s:
        return None
    try:
        parts = [int(p) for p in str(s).split(":")[:3]]
        while len(parts) < 3:
            parts.append(0)
        return dtime(parts[0], parts[1], parts[2])
    except (ValueError, TypeError):
        return None


def _dt(date_str: str, t: dtime) -> datetime:
    y, m, d = (int(x) for x in str(date_str).split("-"))
    return datetime(y, m, d, t.hour, t.minute, t.second, tzinfo=ET)


def _floor_min(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def session_tag(entry_t: dtime | None) -> str | None:
    if entry_t is None:
        return None
    if entry_t < dtime(7, 0):
        return "session_early_premarket"
    if entry_t < dtime(9, 30):
        return "session_pre_open"
    if entry_t < dtime(10, 0):
        return "session_open"
    if entry_t < dtime(15, 0):
        return "session_midday"
    if entry_t < dtime(16, 0):
        return "session_power_hour"
    return "session_after_hours"


def hold_tag(entry_t: dtime | None, exit_t: dtime | None) -> str | None:
    if entry_t is None or exit_t is None:
        return None
    secs = (exit_t.hour * 3600 + exit_t.minute * 60 + exit_t.second) - (entry_t.hour * 3600 + entry_t.minute * 60 + entry_t.second)
    if secs < 0:
        return None
    if secs < 60:
        return "hold_under_1m"
    if secs < 300:
        return "hold_1_5m"
    if secs < 1800:
        return "hold_5_30m"
    return "hold_30m_plus"


def price_tag(entry_price) -> str | None:
    try:
        px = float(entry_price)
    except (TypeError, ValueError):
        return None
    if px <= 0:
        return None
    if px < 2:
        return "price_under_2"
    if px < 5:
        return "price_2_5"
    if px < 10:
        return "price_5_10"
    return "price_10_plus"


def _norm(t: dict) -> dict:
    """Accept either DB-style (entry_time) or pipeline-style ("Entry Time") keys."""
    return {
        "id": _g(t, "id"),
        "symbol": str(_g(t, "symbol", "Symbol", default="")).upper(),
        "trade_date": _g(t, "trade_date", "Trade Date"),
        "entry_time": _g(t, "entry_time", "Entry Time"),
        "exit_time": _g(t, "exit_time", "Exit Time"),
        "entry_price": _g(t, "entry_price", "Entry Price"),
        "exit_price": _g(t, "exit_price", "Exit Price"),
        "shares": _g(t, "shares", "No. of Shares"),
        "pnl": _g(t, "pnl_after_comm", "P&L After Comm"),
        "side": str(_g(t, "side", "Side", default="long")).lower(),
        "fills": _g(t, "fills", "Fills", default=None) or [],
    }


def _groups(fills: list, order_key: str, time_key: str, price_key: str) -> list[tuple]:
    """[(first_time, qty, avg_price)] per distinct broker order on one side, oldest first."""
    by: dict = {}
    for f in fills:
        if not isinstance(f, dict):
            continue
        qty = float(f.get("qty") or 0)
        px = f.get(price_key)
        if qty <= 0 or px is None:
            continue
        key = f.get(order_key) or (f.get(time_key), px)
        g = by.setdefault(key, {"t": f.get(time_key) or "", "q": 0.0, "pq": 0.0})
        g["q"] += qty
        g["pq"] += qty * float(px)
    return sorted(((g["t"], g["q"], g["pq"] / g["q"]) for g in by.values()), key=lambda x: x[0])


def _fill_tags(fills: list) -> list[str]:
    out = []
    entries = _groups(fills, "entry_order", "entry_time", "entry_price")
    exits = _groups(fills, "exit_order", "exit_time", "exit_price")
    if len(entries) > 1:
        out.append("scaled_in")
        q, c = entries[0][1], entries[0][1] * entries[0][2]
        for _t, gq, gp in entries[1:]:
            if gp < c / q - PRICE_EPS:
                out.append("added_to_loser")
                break
            q += gq
            c += gq * gp
    if len(exits) > 1:
        out.append("scaled_out")
    return out


def _day_tags(me: dict, day_trades: list[dict], median_shares) -> list[str]:
    out = []
    day = sorted((_norm(t) for t in day_trades), key=lambda t: (t["entry_time"] or "", str(t["id"])))
    if not any(t["id"] == me["id"] for t in day):
        day.append(me)
        day.sort(key=lambda t: (t["entry_time"] or "", str(t["id"])))
    pos = next((i for i, t in enumerate(day) if t["id"] == me["id"]), None)
    if pos is None:
        return out
    if pos + 1 >= OVERTRADING_FROM:
        out.append("overtrading")
    same = [t for t in day if t["symbol"] == me["symbol"]]
    spos = next((i for i, t in enumerate(same) if t["id"] == me["id"]), 0)
    if spos + 1 >= REPEAT_SYMBOL_FROM:
        out.append("repeat_symbol")
    # revenge: the previous trade in this symbol lost and I was back in within the window
    if spos > 0:
        prev = same[spos - 1]
        pe, my_in = _parse_t(prev["exit_time"]), _parse_t(me["entry_time"])
        if prev["pnl"] is not None and float(prev["pnl"]) < 0 and pe and my_in:
            gap = (_dt(me["trade_date"], my_in) - _dt(me["trade_date"], pe)).total_seconds() / 60
            if 0 <= gap <= REVENGE_WINDOW_MIN:
                out.append("revenge_trade")
    if median_shares and me["shares"] and float(me["shares"]) > OVERSIZED_MULT * float(median_shares):
        out.append("oversized")
    before = day[:pos]
    if before:
        prev = before[-1]
        if (prev["pnl"] is not None and float(prev["pnl"]) < 0 and prev["shares"] and me["shares"]
                and float(me["shares"]) >= SIZE_UP_MULT * float(prev["shares"])):
            out.append("sized_up_after_loss")
    if len(before) >= LOSING_STREAK_LEN and all(
            t["pnl"] is not None and float(t["pnl"]) < 0 for t in before[-LOSING_STREAK_LEN:]):
        out.append("after_losing_streak")
    my_in = _parse_t(me["entry_time"])
    if my_in and me["trade_date"]:
        my_in_dt = _dt(me["trade_date"], my_in)
        exits = []
        for t in before:
            xt = _parse_t(t["exit_time"])
            if xt:
                xd = _dt(me["trade_date"], xt)
                if xd <= my_in_dt:
                    exits.append(xd)
        if exits and 0 <= (my_in_dt - max(exits)).total_seconds() <= RAPID_FIRE_SEC:
            out.append("rapid_fire")
    return out


def _bar_tags(me: dict, bars) -> list[str]:
    out = []
    if bars is None or len(bars) == 0 or me["side"] == "short":
        return out
    et, xt = _parse_t(me["entry_time"]), _parse_t(me["exit_time"])
    if et is None or me["entry_price"] is None:
        return out
    entry_min = _floor_min(_dt(me["trade_date"], et))
    entry_px = float(me["entry_price"])

    prior = bars.loc[bars.index < entry_min]
    if len(prior):
        last = prior.iloc[-1]
        if "EMA9" in prior.columns and float(last["EMA9"]) > 0 and entry_px < float(last["EMA9"]) - PRICE_EPS:
            out.append("below_ema9")
        if "VWAP" in prior.columns and float(last["VWAP"]) > 0 and entry_px < float(last["VWAP"]) - PRICE_EPS:
            out.append("below_vwap")
        if "EMA9" in prior.columns and float(last["EMA9"]) > 0 and entry_px >= float(last["EMA9"]) * (1 + EXTENDED_ABOVE_EMA_PCT):
            out.append("extended_entry")
        if len(prior) >= SPIKE_BARS:
            w = prior.tail(SPIKE_BARS)
            o, c = float(w.iloc[0]["Open"]), float(w.iloc[-1]["Close"])
            if o > 0 and c / o - 1 >= SPIKE_PCT:
                out.append("entry_after_spike")
        if len(prior) >= NHOD_MIN_BARS and entry_px >= float(prior["High"].max()) - PRICE_EPS:
            out.append("new_high_of_day")
        open_dt = _dt(me["trade_date"], dtime(9, 30))
        if entry_min >= open_dt:
            pm, reg = prior.loc[prior.index < open_dt], prior.loc[prior.index >= open_dt]
            if len(pm):
                pm_high = float(pm["High"].max())
                if pm_high > 0 and entry_px >= pm_high - PRICE_EPS and (len(reg) == 0 or float(reg["High"].max()) <= pm_high + PRICE_EPS):
                    out.append("premarket_high_break")
        if "VWAP" in prior.columns and len(prior) >= 2:
            look = prior.tail(VWAP_RECLAIM_LOOKBACK + 1)
            lv = float(look.iloc[-1]["VWAP"])
            if (lv > 0 and float(look.iloc[-1]["Close"]) > lv and entry_px >= lv - PRICE_EPS
                    and bool((look.iloc[:-1]["Close"] < look.iloc[:-1]["VWAP"]).any())):
                out.append("vwap_reclaim")

    if xt is None or me["exit_price"] is None:
        return out
    exit_min = _floor_min(_dt(me["trade_date"], xt))
    exit_px = float(me["exit_price"])
    window = bars.loc[(bars.index >= entry_min) & (bars.index <= exit_min)]
    if len(window):
        if entry_px > 0 and (entry_px - float(window["Low"].min())) / entry_px >= DRAWDOWN_PCT:
            out.append("deep_drawdown")
        peak = float(window["High"].max())
        peak_t = window["High"].idxmax()
        gain = peak - entry_px
        if entry_px > 0 and gain / entry_px >= PEAK_MIN_PCT:
            gave_back = (peak - exit_px) / gain
            if exit_px > entry_px and gave_back <= 1 - NEAR_HIGH_KEEP_FRAC:
                out.append("sold_near_high")
            if gave_back >= GAVE_BACK_FRAC:
                out.append("gave_back_winner")
            lost = (float(me["pnl"]) < 0) if me["pnl"] is not None else exit_px < entry_px
            if lost:
                out.append("winner_turned_loser")
            mins_after_peak = (exit_min - _floor_min(peak_t)).total_seconds() / 60
            if mins_after_peak >= OVERSTAY_MIN_AFTER_PEAK and gave_back >= OVERSTAY_GIVEBACK_FRAC:
                out.append("overstayed")
    after = bars.loc[(bars.index > exit_min) & (bars.index <= exit_min + timedelta(minutes=RAN_AFTER_EXIT_WINDOW_MIN))]
    if len(after) and exit_px > 0 and float(after["High"].max()) >= exit_px * (1 + RAN_AFTER_EXIT_PCT):
        out.append("ran_after_exit")
    if exit_min in bars.index and exit_px > 0:
        xb = bars.loc[exit_min]
        if getattr(xb, "ndim", 1) > 1:
            xb = xb.iloc[-1]
        rng = float(xb["High"]) - float(xb["Low"])
        if rng >= 0.01 and (exit_px - float(xb["Low"])) / rng <= SOLD_LOW_BAR_FRAC:
            soon = bars.loc[(bars.index > exit_min) & (bars.index <= exit_min + timedelta(minutes=SOLD_LOW_WINDOW_MIN))]
            if len(soon) and float(soon["High"].max()) >= exit_px * (1 + SOLD_LOW_RECOVER_PCT):
                out.append("sold_the_low")
    return out


def _has_valid_setup(me: dict, bars) -> bool:
    """True when the entry matches one of the three planned setups (a chase does not count).
    Needs the bars; without them we can't say, so the trade is simply not called clean."""
    if bars is None or len(bars) == 0 or me["entry_price"] is None:
        return False
    et = _parse_t(me["entry_time"])
    if et is None or not me["trade_date"]:
        return False
    try:
        import setups
        found = setups.matches(bars, _dt(me["trade_date"], et), float(me["entry_price"]), "long")
    except Exception:
        return False
    return any(m in (setups.RED_TO_GREEN, setups.GREEN_OVER_GREEN_WICK, setups.EMA9_DIP_BUY) for m in found)


def compute_tags(trade: dict, day_trades: list[dict] | None = None, bars=None,
                 median_shares: float | None = None) -> list[str]:
    """Every behavior tag that applies to one trade, in a stable order."""
    me = _norm(trade)
    out: list[str] = []
    s = session_tag(_parse_t(me["entry_time"]))
    if s:
        out.append(s)
    for extra in (hold_tag(_parse_t(me["entry_time"]), _parse_t(me["exit_time"])), price_tag(me["entry_price"])):
        if extra:
            out.append(extra)
    if me["side"] != "short":
        out += _fill_tags(me["fills"])
        out += _day_tags(me, day_trades or [], median_shares)
        out += _bar_tags(me, bars)
        if not NOT_CLEAN & set(out) and _has_valid_setup(me, bars):
            out.append("clean_trade")
    order = {t: i for i, t in enumerate(ALL_TAGS)}
    return sorted(set(out), key=lambda t: order.get(t, 99))


# ---------------------------------------------------------------------------
# New imports: gather the day's other trades + typical size from Supabase, then tag.
# Best effort -- returns None (so the trade is simply "not tagged yet" and the Journal's
# "Tag my setups" backfill picks it up) rather than ever blocking a publish.
# ---------------------------------------------------------------------------
def tags_for_new_trade(trade: dict, user_id) -> list[str] | None:
    try:
        import requests
        from publish import SUPABASE_URL, _supabase_headers
        from market_data import get_full_day_bars

        me = _norm(trade)
        if not me["trade_date"] or not user_id:
            return None
        hdr = _supabase_headers()
        day = requests.get(f"{SUPABASE_URL}/rest/v1/trades", headers=hdr, timeout=20, params={
            "user_id": f"eq.{user_id}", "trade_date": f"eq.{me['trade_date']}",
            "select": "id,symbol,entry_time,exit_time,pnl_after_comm,shares"})
        day.raise_for_status()
        sizes = requests.get(f"{SUPABASE_URL}/rest/v1/trades", headers=hdr, timeout=20, params={
            "user_id": f"eq.{user_id}", "select": "shares", "order": "trade_date.desc", "limit": "500"})
        sizes.raise_for_status()
        shares = [float(r["shares"]) for r in sizes.json() if r.get("shares")]
        med = median(shares) if len(shares) >= MEDIAN_MIN_TRADES else None
        t2 = dict(trade)
        t2["id"] = me["id"] or f"{me['symbol']}-{str(me['trade_date']).replace('-', '')}-{str(me['entry_time']).replace(':', '')}"
        bars = None
        try:
            bars = get_full_day_bars(me["symbol"], me["trade_date"])
        except Exception:
            bars = None
        return compute_tags(t2, day.json(), bars, med)
    except Exception:
        return None

"""What every auto tag means, in plain English, built from the SAME constants the tagger uses.

The Tag guide on the site (tags.html) and the tap-a-tag sheet read this through GET /tag-catalog,
so the criteria shown on screen can never drift from what trade_tags.py / setups.py really do:
change a threshold constant and the text changes with it. tests/test_tag_catalog.py fails if a
tag exists in the tagger without an entry here (or the reverse).

Each entry: {key, label, group, tone, summary, rule}
  tone: "good"  = something to repeat
        "watch" = worth a look when reviewing (not always a mistake)
        "info"  = just describes the trade
"""
from __future__ import annotations

import setups as S
import trade_tags as T


def _pct(x: float) -> str:
    return f"{x * 100:g}%"


GROUPS = [
    {"id": "process", "label": "Process", "blurb": "Did the trade follow the plan?"},
    {"id": "setup", "label": "Setup", "blurb": "Which of your planned entries this was. Exactly one per trade."},
    {"id": "session", "label": "Session", "blurb": "When you entered (ET)."},
    {"id": "hold", "label": "Hold time", "blurb": "Entry to exit."},
    {"id": "price", "label": "Price", "blurb": "Entry price bracket."},
    {"id": "entry", "label": "Entry shape", "blurb": "Read from the 1-minute bars at the moment you bought."},
    {"id": "exit", "label": "In the trade & exit", "blurb": "What price did while you held, and after you sold."},
    {"id": "size", "label": "Size & fills", "blurb": "How you built and sized the position."},
    {"id": "day", "label": "Day behavior", "blurb": "How this trade sits among the rest of the day's trades."},
    {"id": "context", "label": "Stock context", "blurb": "Volume and float of the stock itself."},
]


def _e(key, label, group, tone, summary, rule):
    return {"key": key, "label": label, "group": group, "tone": tone, "summary": summary, "rule": rule}


def _entries() -> list[dict]:
    a = S.MAX_ABOVE_TRIGGER_PCT
    allowance = f"{_pct(a)} above that high, at least 1c (about {a * 200:.0f}c on a $2 stock, {a * 500:.0f}c on a $5 one)"
    bars_note = "Needs the 1-minute bars; without them the tag is simply not applied."
    out = [
        _e("clean_trade", "Clean trade", "process", "good",
           "A planned setup with no process problems.",
           "The entry matches red-to-green, green-over-green wick or 9 EMA dip buy (a chase does not count) AND the trade has none of: "
           "added to loser, revenge trade, overtrading, oversized, extended entry, sized up after loss, after losing streak. "
           "It judges process only, never the P&L. " + bars_note + " Oversized is only judged once you have "
           f"{T.MEDIAN_MIN_TRADES}+ trades of history."),

        _e(S.RED_TO_GREEN, "Red-to-green", "setup", "info",
           "Bought the break over a red candle's high.",
           f"The last completed 1-min bar before your entry closed red (close < open) and your entry filled at or above that bar's high, "
           f"but no more than {allowance}. The prior bar must be within {S.MAX_GAP_MIN} min of your entry. "
           "The entry bar itself does not have to close green. If several setups match, this one wins."),
        _e(S.GREEN_OVER_GREEN_WICK, "Green-over-green wick", "setup", "info",
           "Bought over the wick of a green candle.",
           f"The last completed bar was green with an upper wick of at least {_pct(S.WICK_MIN_FRAC)} of its full range "
           f"(and at least {S.WICK_MIN_ABS * 100:g}c), and your entry filled above that bar's high, "
           f"within the same allowance ({_pct(a)} above it, at least 1c)."),
        _e(S.EMA9_DIP_BUY, "9 EMA dip buy", "setup", "info",
           "Bought the bounce off the 9 EMA.",
           f"In the last {S.EMA_LOOKBACK_BARS} bars (or the entry bar) the low reached the 9 EMA (at or under it plus {_pct(S.EMA_TOUCH_PCT)}, minimum 1c), "
           f"and before that dip price had been riding above it (at least {S.EMA_TREND_MIN_ABOVE} of the {S.EMA_TREND_BARS} bars before the dip closed over the EMA). "
           f"Your fill is between {_pct(S.EMA_ENTRY_MAX_BELOW_PCT)} below and {_pct(S.EMA_ENTRY_MAX_ABOVE_PCT)} above the EMA."),
        _e(S.CHASE, "Chase", "setup", "watch",
           "Right pattern, but you paid up for it.",
           f"A red-to-green or green-over-green wick pattern was there, but your fill was more than {_pct(a)} above the prior bar's high, "
           "and no other setup fits. Tagged so a late entry stands out instead of hiding in Other."),
        _e(S.OTHER, "Other", "setup", "info",
           "None of your planned setups.",
           "None of the setups above matched. Every short trade is Other too, since the plan is buying long."),

        _e("session_early_premarket", "Early premarket (4–7)", "session", "info", "Entered 04:00–07:00 ET.", "Entry time from 04:00 up to 07:00 ET."),
        _e("session_pre_open", "Pre-open (7–9:30)", "session", "info", "Entered 07:00–09:30 ET.", "Entry time from 07:00 up to 09:30 ET."),
        _e("session_open", "Open (9:30–10)", "session", "info", "Entered 09:30–10:00 ET.", "Entry time from 09:30 up to 10:00 ET."),
        _e("session_midday", "Midday (10–15)", "session", "info", "Entered 10:00–15:00 ET.", "Entry time from 10:00 up to 15:00 ET."),
        _e("session_power_hour", "Power hour (15–16)", "session", "info", "Entered 15:00–16:00 ET.", "Entry time from 15:00 up to 16:00 ET."),
        _e("session_after_hours", "After hours", "session", "info", "Entered after 16:00 ET.", "Entry time 16:00 ET or later."),

        _e("hold_under_1m", "Held < 1 min", "hold", "info", "Out in under a minute.", "Entry to exit under 60 seconds."),
        _e("hold_1_5m", "Held 1–5 min", "hold", "info", "Held 1 to 5 minutes.", "Entry to exit from 60 seconds up to 5 minutes."),
        _e("hold_5_30m", "Held 5–30 min", "hold", "info", "Held 5 to 30 minutes.", "Entry to exit from 5 up to 30 minutes."),
        _e("hold_30m_plus", "Held 30+ min", "hold", "info", "Held half an hour or more.", "Entry to exit of 30 minutes or longer."),

        _e("price_under_2", "Price under $2", "price", "info", "Entry price under $2.", "Entry price below $2.00."),
        _e("price_2_5", "Price $2–5", "price", "info", "Entry price $2 to $5.", "Entry price from $2.00 up to $5.00."),
        _e("price_5_10", "Price $5–10", "price", "info", "Entry price $5 to $10.", "Entry price from $5.00 up to $10.00."),
        _e("price_10_plus", "Price $10+", "price", "info", "Entry price $10 or more.", "Entry price $10.00 or higher."),

        _e("below_vwap", "Entry below VWAP", "entry", "watch", "Bought under VWAP.",
           f"Your entry filled more than {T.PRICE_EPS * 100:g}c under VWAP, read at the last completed bar before entry. " + bars_note),
        _e("below_ema9", "Entry below 9 EMA", "entry", "watch", "Bought under the 9 EMA.",
           f"Your entry filled more than {T.PRICE_EPS * 100:g}c under the 9 EMA, read at the last completed bar before entry. " + bars_note),
        _e("extended_entry", "Extended entry", "entry", "watch", "Bought well above the 9 EMA.",
           f"Your entry filled {_pct(T.EXTENDED_ABOVE_EMA_PCT)} or more above the 9 EMA at the last completed bar. " + bars_note),
        _e("entry_after_spike", "Entry after spike", "entry", "watch", "Bought right after a fast run-up.",
           f"The {T.SPIKE_BARS} completed bars before your entry ran up {_pct(T.SPIKE_PCT)} or more, from the first bar's open to the last bar's close. " + bars_note),
        _e("new_high_of_day", "New high of day", "entry", "info", "Bought a break of the day's high.",
           f"Your entry filled at or over the highest high of every bar so far that day (premarket included). Needs {T.NHOD_MIN_BARS}+ bars of the day before the entry. " + bars_note),
        _e("premarket_high_break", "Premarket high break", "entry", "info", "Bought the first break of the premarket high.",
           "You entered after 09:30 ET at or over the premarket high, and no regular-session bar before your entry had already traded over it. " + bars_note),
        _e("vwap_reclaim", "VWAP reclaim", "entry", "info", "Bought as price got back over VWAP.",
           f"Within the last {T.VWAP_RECLAIM_LOOKBACK} bars before the last completed one, at least one closed under VWAP; "
           "the last completed bar closed back over it and you entered at or over VWAP. " + bars_note),

        _e("gave_back_winner", "Gave back winner", "exit", "watch", "Was up, then gave back most of it.",
           f"The trade peaked at least {_pct(T.PEAK_MIN_PCT)} over your entry, then you exited having given back {_pct(T.GAVE_BACK_FRAC)} or more of that peak gain. " + bars_note),
        _e("winner_turned_loser", "Winner turned loser", "exit", "watch", "Was a winner, closed as a loss.",
           f"The trade peaked at least {_pct(T.PEAK_MIN_PCT)} over your entry, and you still closed it at a loss. " + bars_note),
        _e("overstayed", "Overstayed", "exit", "watch", "Stayed well past the peak.",
           f"You held {T.OVERSTAY_MIN_AFTER_PEAK}+ minutes past the peak (a peak at least {_pct(T.PEAK_MIN_PCT)} over entry), "
           f"having given back {_pct(T.OVERSTAY_GIVEBACK_FRAC)} or more of the peak gain. " + bars_note),
        _e("ran_after_exit", "Ran after exit", "exit", "watch", "It kept running after you sold.",
           f"Within {T.RAN_AFTER_EXIT_WINDOW_MIN} minutes of your exit, price traded {_pct(T.RAN_AFTER_EXIT_PCT)} or more above your exit price. " + bars_note),
        _e("sold_the_low", "Sold the low", "exit", "watch", "Sold the bottom of a candle, then it bounced.",
           f"Your exit filled in the bottom {_pct(T.SOLD_LOW_BAR_FRAC)} of its 1-minute bar's range, and within {T.SOLD_LOW_WINDOW_MIN} minutes "
           f"price traded {_pct(T.SOLD_LOW_RECOVER_PCT)} or more above your exit. " + bars_note),
        _e("sold_near_high", "Sold near the high", "exit", "good", "Kept most of the move.",
           f"The trade peaked at least {_pct(T.PEAK_MIN_PCT)} over your entry and you exited above entry having kept {_pct(T.NEAR_HIGH_KEEP_FRAC)} or more of the peak gain. " + bars_note),
        _e("deep_drawdown", "Deep drawdown", "exit", "watch", "Went well against you while you held.",
           f"Price traded {_pct(T.DRAWDOWN_PCT)} or more below your entry at some point between entry and exit (win or lose). " + bars_note),

        _e("scaled_in", "Scaled in", "size", "info", "More than one buy order.", "The trade has more than one entry order (separate broker orders on the buy side)."),
        _e("scaled_out", "Scaled out", "size", "info", "More than one sell order.", "The trade has more than one exit order (separate broker orders on the sell side)."),
        _e("added_to_loser", "Added to loser", "size", "watch", "Bought more while it was under water.",
           "A later buy filled below the average cost of your earlier buys in the same trade."),
        _e("oversized", "Oversized", "size", "watch", "Bigger than your usual size.",
           f"Shares are more than {T.OVERSIZED_MULT:g}x the median size of all your trades. Not judged until you have {T.MEDIAN_MIN_TRADES}+ trades."),

        _e("revenge_trade", "Revenge trade", "day", "watch", "Jumped back into a stock right after losing in it.",
           f"The previous trade that day in this same symbol lost, and you entered again within {T.REVENGE_WINDOW_MIN} minutes of its exit."),
        _e("overtrading", "Overtrading", "day", "watch", "The 10th trade of the day, or later.",
           f"This is trade number {T.OVERTRADING_FROM} or later that day (by entry time)."),
        _e("repeat_symbol", "Repeat symbol", "day", "watch", "The 3rd+ trade in the same stock that day.",
           f"This is the {T.REPEAT_SYMBOL_FROM}rd or later trade in the same symbol that day (by entry time)."),
        _e("sized_up_after_loss", "Sized up after loss", "day", "watch", "Bigger right after a loser.",
           f"The previous trade of the day lost and this one is {T.SIZE_UP_MULT:g}x its share count or more."),
        _e("after_losing_streak", "After 3 losses in a row", "day", "watch", "Kept trading through a losing streak.",
           f"The {T.LOSING_STREAK_LEN} trades right before this one that day all lost."),
        _e("rapid_fire", "Rapid-fire re-entry", "day", "watch", "Back in within a minute of your last exit.",
           f"You entered within {T.RAPID_FIRE_SEC} seconds of your latest exit that day (any symbol)."),
    ]

    ctx_note = " Computed when the trade is published or charted."
    avg = [("avgvol_under_500k", "Avg vol < 500K", "under 500,000"), ("avgvol_500k_1m", "Avg vol 500K–1M", "500,000 to 1M"),
           ("avgvol_1m_5m", "Avg vol 1M–5M", "1M to 5M"), ("avgvol_5m_20m", "Avg vol 5M–20M", "5M to 20M"),
           ("avgvol_20m_plus", "Avg vol 20M+", "20M or more")]
    for k, lbl, rng in avg:
        out.append(_e(k, lbl, "context", "info", f"Average daily volume {rng}.",
                      f"The stock's average daily volume over the 30 trading days before the trade was {rng} shares." + ctx_note))
    out.append(_e("avgvol_unknown", "Avg vol unknown", "context", "info", "No average volume available.", "There wasn't enough daily history to work out an average." + ctx_note))
    rv = [("rvol_under_1x", "RVol < 1x", "below 1x"), ("rvol_1x_2x", "RVol 1x–2x", "1x to 2x"), ("rvol_2x_5x", "RVol 2x–5x", "2x to 5x"),
          ("rvol_5x_10x", "RVol 5x–10x", "5x to 10x"), ("rvol_10x_plus", "RVol 10x+", "10x or more")]
    for k, lbl, rng in rv:
        out.append(_e(k, lbl, "context", "info", f"Relative volume {rng}.",
                      f"The stock's total volume that day divided by its 30-day average daily volume was {rng}." + ctx_note))
    out.append(_e("rvol_unknown", "RVol unknown", "context", "info", "No relative volume available.", "Volume or the 30-day average wasn't available." + ctx_note))
    fl = [("float_micro_under_10m", "Float < 10M", "under 10M"), ("float_low_10m_20m", "Float 10M–20M", "10M to 20M"),
          ("float_mid_20m_50m", "Float 20M–50M", "20M to 50M"), ("float_large_50m_200m", "Float 50M–200M", "50M to 200M"),
          ("float_mega_200m_plus", "Float 200M+", "200M or more")]
    for k, lbl, rng in fl:
        out.append(_e(k, lbl, "context", "info", f"Float {rng} shares.", f"The stock's float is {rng} shares. Filled in when you tap Get float on a trade or run Fill missing floats."))
    out.append(_e("float_unknown", "Float unknown", "context", "info", "Float not looked up yet.", "Float is only fetched on demand (Get float on a trade, or Fill missing floats in the Journal)."))
    return out


def build() -> dict:
    return {
        "groups": GROUPS,
        "tags": _entries(),
        "ai_note": "Tags like these in the Journal's Tags column that aren't in this guide come from the AI review of that trade. "
                   "Open the trade to read the lesson behind each one.",
    }


def expected_keys() -> set[str]:
    """Every tag the tagger can emit: behavior tags, setups, and the stock-context buckets."""
    ctx = {e["key"] for e in _entries() if e["group"] == "context"}
    return set(T.ALL_TAGS) | set(S.SETUP_TYPES) | ctx


def write_snapshot(js_path: str) -> None:
    """Refresh the built-in copy inside the website's js/tag-catalog.js (the line `var SNAPSHOT = {...};`),
    so the page has the right text even before it can reach this service.
        python tag_catalog.py ../web-service/js/tag-catalog.js
    """
    import json
    import re

    with open(js_path, encoding="utf-8") as f:
        src = f.read()
    snap = json.dumps(build(), ensure_ascii=False, separators=(",", ":"))
    new, n = re.subn(r"^  var SNAPSHOT = .*;$", lambda _m: "  var SNAPSHOT = " + snap + ";", src, count=1, flags=re.M)
    if n != 1:
        raise SystemExit("couldn't find the `var SNAPSHOT = ...;` line in " + js_path)
    with open(js_path, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"updated {js_path} ({len(build()['tags'])} tags)")


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python tag_catalog.py <path to js/tag-catalog.js>")
    write_snapshot(sys.argv[1])

"""
charts.py
Chart rendering (matplotlib/mplfinance) and the /generate-chart,
/generate-daily-chart, /full-day-bars, and /tick-data routes. Split out of
the old chart_service.py -- see chart_service.py's module docstring for how
these files fit together.
"""

import io
import time
import base64
import traceback
from datetime import datetime, timedelta
from concurrent.futures import TimeoutError as FutureTimeoutError

import pandas as pd
import mplfinance as mpf
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import MaxNLocator, AutoMinorLocator
from flask import request, jsonify

from config import app, log, RENDER_LOCK, REQUEST_HARD_TIMEOUT_S, _request_pool, ET, SR_LOOKBACK_DAYS_DEFAULT
from market_data import (
    fetch_bars, get_full_day_bars, compute_indicators, compute_trade_levels,
    compute_volume_float_stats, nearest_row, serialize_bars,
    _fetch_daily_bars_from_polygon, _find_pivot_levels, _fetch_trade_ticks,
)

def render_chart(df: pd.DataFrame, symbol: str, entry_dt, exit_dt, entry_price, exit_price,
                  vwap_at_entry, ema9_at_entry, ema20_at_entry, ema200_at_entry,
                  stop_price=None, target_price=None,
                  better_entry=None, better_exit=None) -> bytes:
    # Histogram bars colored per-bar: green when positive, red when negative.
    macd_hist_colors = ["#2ca02c" if v >= 0 else "#d62728" for v in df["MACD_hist"]]
    # MACD lives on its own panel (2), separate from volume (panel 1) -- both
    # were previously defaulting to panel 1 at once (volume's implicit default
    # collided with MACD's explicit panel=1), which is why MACD was rendering
    # on top of the volume bars instead of in its own lane below them.
    macd_panel = [
        mpf.make_addplot(df["MACD"], panel=2, color="blue", ylabel="MACD", width=1.0),
        mpf.make_addplot(df["MACD_signal"], panel=2, color="orange", width=1.0),
        mpf.make_addplot(df["MACD_hist"], panel=2, type="bar", color=macd_hist_colors, width=0.7, alpha=0.75),
    ]
    overlays = [
        mpf.make_addplot(df["VWAP"], color="orange", width=1.3),
        mpf.make_addplot(df["EMA9"], color="gray", width=1.0),
        mpf.make_addplot(df["EMA20"], color="blue", width=1.0),
        mpf.make_addplot(df["EMA200"], color="purple", width=1.0),
    ]

    style = mpf.make_mpf_style(base_mpf_style="yahoo", gridstyle="")

    # mpf.plot / plt.close touch matplotlib's process-global state, which
    # isn't safe to hit from multiple threads at once -- serialize the whole
    # render under RENDER_LOCK. try/finally guarantees the lock is released
    # even if something raises mid-render, so one failed chart can't
    # permanently wedge every request behind it.
    RENDER_LOCK.acquire()
    try:
        return _render_chart_locked(
            df, symbol, entry_dt, exit_dt, entry_price, exit_price,
            macd_panel, overlays, style,
            vwap_at_entry, ema9_at_entry, ema20_at_entry, ema200_at_entry,
            stop_price, target_price, better_entry, better_exit,
        )
    finally:
        RENDER_LOCK.release()

def _render_chart_locked(df, symbol, entry_dt, exit_dt, entry_price, exit_price,
                          macd_panel, overlays, style,
                          vwap_at_entry, ema9_at_entry, ema20_at_entry, ema200_at_entry,
                          stop_price=None, target_price=None,
                          better_entry=None, better_exit=None) -> bytes:
    """Everything here runs with RENDER_LOCK held -- see render_chart()."""
    fig, axes = mpf.plot(
        df,
        type="candle",
        style=style,
        addplot=overlays + macd_panel,
        volume=True,
        volume_panel=1,
        # price : volume : macd -- volume and MACD each get a smaller slice
        # than a plain (3, 1, 1) split so they stop crowding each other now
        # that they're on their own panels.
        panel_ratios=(4, 1, 1),
        returnfig=True,
        figsize=(12, 8.2),
        title=f"{symbol} — trade review",
        datetime_format="%H:%M",
        xrotation=0,
    )

    price_ax = axes[0]
    price_ax.yaxis.set_major_locator(MaxNLocator(nbins=14, prune=None))
    price_ax.yaxis.set_minor_locator(AutoMinorLocator(2))
    price_ax.grid(True, which="major", axis="y", linestyle="--", linewidth=0.6, color="#999999", alpha=0.55)
    price_ax.grid(True, which="minor", axis="y", linestyle=":", linewidth=0.4, color="#cccccc", alpha=0.35)
    price_ax.grid(True, which="major", axis="x", linestyle="--", linewidth=0.4, color="#cccccc", alpha=0.25)

    # mplfinance addplots don't auto-populate a legend — build one explicitly.
    # The line color itself is enough to tell VWAP/EMA9/EMA20/EMA200 apart,
    # so the label just carries each one's price at entry instead of
    # spelling out the color or the VWAP session-reset note.
    legend_lines = [
        Line2D([0], [0], color="orange", lw=1.3, label=f"VWAP  ${vwap_at_entry:.2f}"),
        Line2D([0], [0], color="gray", lw=1.0, label=f"EMA 9  ${ema9_at_entry:.2f}"),
        Line2D([0], [0], color="blue", lw=1.0, label=f"EMA 20  ${ema20_at_entry:.2f}"),
        Line2D([0], [0], color="purple", lw=1.0, label=f"EMA 200  ${ema200_at_entry:.2f}"),
    ]
    # Placed above the axes (not inside upper-left corner) so it can never
    # collide with the entry/exit labels, which also live near the top.
    price_ax.legend(
        handles=legend_lines, loc="lower center", bbox_to_anchor=(0.5, 1.01),
        ncol=4, fontsize=8, framealpha=0.9, borderaxespad=0,
    )

    entry_x = df.index.get_indexer([entry_dt], method="nearest")[0]
    exit_x = df.index.get_indexer([exit_dt], method="nearest")[0]

    price_ax.axhline(entry_price, color="green", linestyle=":", linewidth=0.8, alpha=0.6)
    price_ax.axhline(exit_price, color="red", linestyle=":", linewidth=0.8, alpha=0.6)

    price_high = df["High"].max()
    price_low = df["Low"].min()
    price_range = price_high - price_low

    # Anchor each label to the candle highs right around ITS OWN arrow,
    # instead of a fixed height above the whole chart's peak -- that's what
    # keeps the label close to the arrow instead of floating way up top.
    def _local_high(center_x, half_window=4):
        lo = max(0, center_x - half_window)
        hi = min(len(df) - 1, center_x + half_window)
        return float(df["High"].iloc[lo:hi + 1].max())

    label_gap = price_range * 0.05      # clearance above the nearest candle tops
    box_half_height = price_range * 0.06  # room the two-line label box needs above its anchor

    entry_label_y = _local_high(entry_x) + label_gap + box_half_height
    exit_label_y = _local_high(exit_x) + label_gap + box_half_height

    # On fast trades entry_x and exit_x can be only a few bars apart, which
    # would put both label boxes at nearly the same spot -- push whichever
    # one is lower up above the other so they never collide.
    if abs(entry_x - exit_x) < 10 and abs(entry_label_y - exit_label_y) < box_half_height * 2:
        if entry_label_y <= exit_label_y:
            entry_label_y = exit_label_y + box_half_height * 2
        else:
            exit_label_y = entry_label_y + box_half_height * 2

    # Better entry/exit label positions -- computed HERE, before ylim is set,
    # so their headroom actually gets counted below. (Previously these were
    # computed later inside _mark_better, after ylim was already locked in,
    # so a better-entry/exit label could land above the visible range --
    # and since the figure is saved with bbox_inches="tight", matplotlib
    # would just stretch the whole image upward to reach it, which is what
    # made the marker look like it had jumped somewhere far away.)
    def _better_pos(dt_val, kind):
        if dt_val is None:
            return None
        try:
            ts = pd.Timestamp(dt_val)
            # df.index is tz-aware ET (see compute_indicators/fetch_bars).
            # better_entry_time/better_exit_time come in as naive local-ET
            # strings (same convention as serialize_bars' "t" field), so
            # localize rather than convert -- ts already IS ET wall-clock
            # time, it just doesn't carry the tzinfo yet. Comparing a naive
            # Timestamp against a tz-aware index raises, which used to be
            # swallowed by the except below with no log -- the marker just
            # silently never appeared.
            if ts.tzinfo is None:
                ts = ts.tz_localize(ET)
            x = int(df.index.get_indexer([ts], method="nearest")[0])
        except Exception:
            log.warning("Couldn't place %s better-%s marker for %s at %r", symbol, kind, symbol, dt_val)
            return None
        y = _local_high(x) + label_gap + box_half_height * (3 if kind == "entry" else 3.6)
        return (x, y)

    better_entry_pos = _better_pos(better_entry.get("time"), "entry") if better_entry else None
    better_exit_pos = _better_pos(better_exit.get("time"), "exit") if better_exit else None

    # Headroom needs to cover whichever label ends up highest, plus a small
    # margin -- not a fixed fraction of the whole chart like before.
    candidate_tops = [price_high, entry_label_y + box_half_height, exit_label_y + box_half_height]
    if better_entry_pos is not None:
        candidate_tops.append(better_entry_pos[1] + box_half_height)
    if better_exit_pos is not None:
        candidate_tops.append(better_exit_pos[1] + box_half_height)
    chart_top = max(candidate_tops)
    top_pad = max(price_range * 0.08, (chart_top - price_high) + price_range * 0.04)
    bottom_pad = price_range * 0.06
    price_ax.set_ylim(price_low - bottom_pad, price_high + top_pad)

    # Label text floats up in the headroom band with NO arrow attached to it --
    # this is what used to stretch a long arrow across the whole candle area.
    price_ax.annotate(
        f"ENTRY ${entry_price:.2f}\n{entry_dt.strftime('%H:%M:%S')}",
        xy=(entry_x, entry_label_y), xycoords="data",
        color="green", fontweight="bold", fontsize=8.5, ha="center", va="center",
        bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="green", alpha=0.9),
    )
    price_ax.annotate(
        f"EXIT ${exit_price:.2f}\n{exit_dt.strftime('%H:%M:%S')}",
        xy=(exit_x, exit_label_y), xycoords="data",
        color="red", fontweight="bold", fontsize=8.5, ha="center", va="center",
        bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="red", alpha=0.9),
    )

    # No arrow shaft at all -- just a tiny triangle glyph, tip resting
    # exactly on the price (no hover gap: va="bottom" anchors the bottom of
    # the glyph's own bounding box -- which is where a down-pointing
    # triangle's tip sits -- directly at xy, same principle as the
    # interactive chart's pointer markers).
    price_ax.annotate(
        "\u25bc", xy=(entry_x, entry_price), xycoords="data",
        ha="center", va="bottom", fontsize=9, color="green",
    )
    price_ax.annotate(
        "\u25bc", xy=(exit_x, exit_price), xycoords="data",
        ha="center", va="bottom", fontsize=9, color="red",
    )

    # Structural / suggested stop & target — dashed reference lines so the
    # levels used to grade the trade are visible on the chart itself.
    if stop_price is not None:
        price_ax.axhline(stop_price, color="#b02a2a", linestyle="--", linewidth=0.9, alpha=0.7)
        price_ax.annotate(f"stop ${stop_price:.2f}", xy=(1, stop_price), xycoords=("axes fraction", "data"),
                           xytext=(4, 0), textcoords="offset points", ha="left", va="center",
                           fontsize=7.5, color="#b02a2a")
    if target_price is not None:
        price_ax.axhline(target_price, color="#1a7a4c", linestyle="--", linewidth=0.9, alpha=0.7)
        price_ax.annotate(f"target ${target_price:.2f}", xy=(1, target_price), xycoords=("axes fraction", "data"),
                           xytext=(4, 0), textcoords="offset points", ha="left", va="center",
                           fontsize=7.5, color="#1a7a4c")

    # Better entry/exit — where the trade SHOULD have been taken, per the
    # review verdict. Colored purple/pink -- distinct from the actual entry
    # (green) / exit (red) rather than a same-hue shade of them, so a
    # "better" marker never reads as a faded copy of the actual fill marker
    # -- and a thin reference line at each price, in the same color, so
    # there's an actual line for the marker to sit on (same idea as the
    # actual entry/exit axhlines above). Kept in sync with the purple/pink
    # used for the interactive chart's better-entry/exit pointers in
    # trade.js. Position was already computed above (and folded into the
    # ylim headroom) -- just draw it here.
    BETTER_COLOR = {"entry": "#8b7cf6", "exit": "#ec6cad"}

    def _mark_better(pos, price_val, kind):
        if pos is None or price_val is None:
            return
        x, y = pos
        color = BETTER_COLOR[kind]
        price_ax.axhline(price_val, color=color, linestyle=":", linewidth=0.8, alpha=0.45)
        price_ax.annotate(
            f"BETTER {kind.upper()}\n${price_val:.2f}",
            xy=(x, y), xycoords="data",
            color=color, fontweight="bold", fontsize=8, ha="center", va="center",
            bbox=dict(boxstyle="round,pad=0.22", fc="white", ec=color, alpha=0.92),
        )
        # Tip resting exactly on the price, same principle as the actual
        # entry/exit markers above: va="top" anchors the top of the glyph's
        # own bounding box -- where an up-pointing triangle's tip sits --
        # directly at xy, with no manual hover-gap offset.
        price_ax.annotate(
            "\u25b2", xy=(x, price_val), xycoords="data",
            ha="center", va="top", fontsize=9, color=color,
        )

    if better_entry:
        _mark_better(better_entry_pos, better_entry.get("price"), "entry")
    if better_exit:
        _mark_better(better_exit_pos, better_exit.get("price"), "exit")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()

def _build_chart_response(body, start):
    """The actual work for one /generate-chart call. Runs inside the worker
    pool so generate_chart() below can enforce a hard wall-clock timeout on it."""
    symbol = body["symbol"]
    trade_date = body["trade_date"]
    log.info("Received request: %s on %s", symbol, trade_date)
    entry_dt = datetime.fromisoformat(f"{trade_date}T{body['entry_time']}").replace(tzinfo=ET)
    exit_dt = datetime.fromisoformat(f"{trade_date}T{body['exit_time']}").replace(tzinfo=ET)
    entry_price = float(body["entry_price"])
    exit_price = float(body["exit_price"])
    side = (body.get("side") or "long").lower()

    full_bars, display_mask = fetch_bars(symbol, trade_date, entry_dt, exit_dt)
    full_bars = compute_indicators(full_bars)
    display_bars = full_bars.loc[display_mask]

    # Computed up front (not after rendering) because the chart legend now
    # shows each indicator's price at entry instead of a color name.
    at_entry = nearest_row(full_bars, entry_dt)
    levels = compute_trade_levels(full_bars, entry_dt, entry_price, side)

    # Optional second-pass fields: once the review verdict is known, the
    # workflow calls this endpoint again with these so the FINAL chart (the
    # one that gets published) shows where the trade should've been taken,
    # not just where it was.
    def _better(price_key, time_key):
        # NOTE: t is already a full "<date>T<time>" timestamp -- it's the
        # verdict's better_entry_time/better_exit_time, which daily_sync.py's
        # prompt requires be copied EXACTLY from one of the bar rows' own "t"
        # values (see _bar_table_for_prompt), and serialize_bars() already
        # writes those as full "%Y-%m-%dT%H:%M:%S" strings, not bare HH:MM:SS.
        # This used to re-prepend trade_date here (f"{trade_date}T{t}"),
        # producing a malformed double-date string like
        # "2026-08-12T2026-08-12T09:59:00" -- dateutil's fallback parser
        # accepted that silently and misread part of it as a UTC offset,
        # so the "better entry/exit" marker either landed on the wrong bar
        # or (once _better_pos's tz mismatch below also kicked in) never
        # rendered at all, with nothing surfaced to say why.
        p, t = body.get(price_key), body.get(time_key)
        if p is None or t is None:
            return None
        return {"price": float(p), "time": t}

    better_entry = _better("better_entry_price", "better_entry_time")
    better_exit = _better("better_exit_price", "better_exit_time")
    stop_for_chart = float(body["suggested_stop"]) if body.get("suggested_stop") is not None else levels["stop_price"]
    target_for_chart = float(body["suggested_target"]) if body.get("suggested_target") is not None else levels["target_price"]

    # The PNG is only ever a fallback for consumers that can't render their
    # own interactive chart from bars/indicators (report.js and trade.js
    # both prefer the interactive one and only fall back to chart_image
    # when it's missing -- see Build Backtest Callback Body's comment).
    # Nothing in this service actually sends the image to an LLM -- the
    # Gemini verdict (daily_sync._get_verdict) and the S/R read
    # (generate-daily-chart, below) both already work off a text summary
    # of the indicators/bars, never pixels. So matplotlib rendering is OFF
    # BY DEFAULT: it's real CPU time under the global RENDER_LOCK for a PNG
    # nothing currently displays or sends anywhere. A caller can still opt
    # in with include_image: true if something new needs it.
    if body.get("include_image", False):
        png_bytes = render_chart(
            display_bars, symbol, entry_dt, exit_dt, entry_price, exit_price,
            vwap_at_entry=float(at_entry["VWAP"]),
            ema9_at_entry=float(at_entry["EMA9"]),
            ema20_at_entry=float(at_entry["EMA20"]),
            ema200_at_entry=float(at_entry["EMA200"]),
            stop_price=stop_for_chart, target_price=target_for_chart,
            better_entry=better_entry, better_exit=better_exit,
        )
    else:
        png_bytes = None

    prior_slice = full_bars["MACD_hist"].loc[:at_entry.name]
    prior_macd_hist = prior_slice.iloc[-2] if len(prior_slice) > 1 else None

    indicators = {
        "vwap_at_entry": round(float(at_entry["VWAP"]), 4),
        "ema9_at_entry": round(float(at_entry["EMA9"]), 4),
        "ema20_at_entry": round(float(at_entry["EMA20"]), 4),
        "ema200_at_entry": round(float(at_entry["EMA200"]), 4),
        "macd_at_entry": round(float(at_entry["MACD"]), 4),
        "macd_signal_at_entry": round(float(at_entry["MACD_signal"]), 4),
        "macd_hist_at_entry": round(float(at_entry["MACD_hist"]), 4),
        "macd_hist_prior_bar": round(float(prior_macd_hist), 4) if prior_macd_hist is not None else None,
        "entry_vs_vwap": "above" if entry_price > at_entry["VWAP"] else "below",
        "entry_vs_ema9": "above" if entry_price > at_entry["EMA9"] else "below",
        "entry_vs_ema20": "above" if entry_price > at_entry["EMA20"] else "below",
        "entry_vs_ema200": "above" if entry_price > at_entry["EMA200"] else "below",
        "setup_type": levels["setup_type"],
        "stop_price": levels["stop_price"],
        "target_price": levels["target_price"],
        "risk_per_share": levels["risk_per_share"],
        "reward_per_share": levels["reward_per_share"],
        "r_multiple": levels["r_multiple"],
        "display_price_low": round(float(display_bars["Low"].min()), 4),
        "display_price_high": round(float(display_bars["High"].max()), 4),
    }

    # Best-effort, never fatal -- see compute_volume_float_stats' own
    # try/except. Daily bars are now resampled from full_bars (already
    # fetched above) instead of a second Polygon call. The one Polygon call
    # this can still add is float shares (_fetch_float_shares), and now
    # that's persisted in symbol_float_shares (see float_shares_store.py),
    # it's only a real cost the first time a symbol is EVER seen across the
    # whole app -- not per-restart, not per-user. Callers (the n8n
    # "Generate Chart" / "Generate Final Chart" nodes) can still set
    # include_volume_stats: false to skip it entirely.
    if body.get("include_volume_stats", True):
        indicators.update(compute_volume_float_stats(symbol, datetime.strptime(trade_date, "%Y-%m-%d").date(), full_bars))
    else:
        indicators.update({
            "volume_on_entry_day": None, "avg_volume_30d": None, "relative_volume": None,
            "float_shares": None, "avg_volume_tag": "avgvol_unknown",
            "rvol_tag": "rvol_unknown", "float_tag": "float_unknown",
        })

    log.info("Done: %s in %.1fs", symbol, time.monotonic() - start)
    return {
        "image_base64": base64.b64encode(png_bytes).decode("utf-8") if png_bytes is not None else None,
        "indicators": indicators,
        "bars": serialize_bars(display_bars),
    }

def render_daily_chart(daily: pd.DataFrame, symbol: str, levels: dict) -> bytes:
    """Simple daily candlestick + volume PNG with the computed S/R clusters
    drawn as horizontal lines -- used only by /generate-daily-chart, which
    is only ever called from the trade site's optional button, never the
    automatic pipeline."""
    with RENDER_LOCK:
        plot_df = daily.rename(columns={"Open": "Open", "High": "High", "Low": "Low", "Close": "Close", "Volume": "Volume"})
        plot_df.index = pd.to_datetime(plot_df.index)

        mc = mpf.make_marketcolors(up="#2fd08a", down="#f2555a", edge="inherit", wick="inherit", volume="inherit")
        style = mpf.make_mpf_style(base_mpf_style="nightclouds", marketcolors=mc,
                                    facecolor="#0d1117", edgecolor="#232830", gridcolor="#1c2127")

        hlines = [lv["price"] for lv in levels.get("support", [])] + [lv["price"] for lv in levels.get("resistance", [])]
        hcolors = (["#2fd08a"] * len(levels.get("support", []))) + (["#f2555a"] * len(levels.get("resistance", [])))

        buf = io.BytesIO()
        fig, _ = mpf.plot(
            plot_df, type="candle", volume=True, style=style,
            title=f"\n{symbol} — {len(plot_df)} prior trading days",
            hlines=dict(hlines=hlines, colors=hcolors, linestyle="--", linewidths=0.9) if hlines else None,
            returnfig=True, figsize=(10, 6),
        )
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf.getvalue()

def _build_daily_chart_response(body: dict) -> dict:
    symbol = body["symbol"]
    trade_date = body["trade_date"]
    lookback_days = int(body.get("lookback_days") or SR_LOOKBACK_DAYS_DEFAULT)
    trade_date_obj = datetime.strptime(trade_date, "%Y-%m-%d").date()

    calendar_lookback = int(lookback_days * 1.6) + 10
    start_date = trade_date_obj - timedelta(days=calendar_lookback)
    # This is the only remaining caller of _fetch_daily_bars_from_polygon --
    # compute_volume_float_stats now resamples daily bars from the minute
    # bars it already has instead (see _resample_daily_from_minute_bars).
    # This route is the manual "S/R" button on the trade page, never the
    # automatic pipeline, so it's fine for it to stay a standalone Polygon
    # call. Only bars strictly before trade_date are used below, so the
    # trader never sees a level informed by the trade day itself.
    daily = _fetch_daily_bars_from_polygon(symbol, start_date, trade_date_obj)
    daily = daily.loc[daily.index < trade_date_obj].tail(lookback_days)

    if daily.empty:
        raise ValueError(f"No prior daily bars found for {symbol} before {trade_date} -- check the ticker and lookback_days")

    levels = _find_pivot_levels(daily)
    # Off by default, same reasoning as _build_chart_response above -- the
    # S/R read sends Gemini a text summary of the daily bars, never this
    # image, and the frontend draws the returned levels on its own
    # interactive chart. Pass include_image: true to opt back in.
    png_bytes = render_daily_chart(daily, symbol, levels) if body.get("include_image", False) else None

    bars = [
        {"t": ts.strftime("%Y-%m-%d"), "o": round(float(r["Open"]), 4), "h": round(float(r["High"]), 4),
         "l": round(float(r["Low"]), 4), "c": round(float(r["Close"]), 4), "v": int(r["Volume"])}
        for ts, r in daily.iterrows()
    ]

    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "bars": bars,
        "computed_levels": levels,
        "image_base64": base64.b64encode(png_bytes).decode("utf-8") if png_bytes is not None else None,
    }

@app.route("/generate-daily-chart", methods=["POST"])
def generate_daily_chart():
    """Optional, on-demand only -- the trade site's 'Support & Resistance
    (AI)' button calls this (via the n8n webhook that also does the LLM
    read), never the automatic daily pipeline. See module docstring."""
    start = time.monotonic()
    try:
        body = request.get_json(force=True)
        if body is None or not body.get("symbol") or not body.get("trade_date"):
            return jsonify({"error": "symbol and trade_date are required"}), 400

        future = _request_pool.submit(_build_daily_chart_response, body)
        try:
            result = future.result(timeout=REQUEST_HARD_TIMEOUT_S)
        except FutureTimeoutError:
            log.error("Hard timeout after %.1fs for daily chart %s", time.monotonic() - start, body.get("symbol"))
            return jsonify({"error": f"generate-daily-chart exceeded the {REQUEST_HARD_TIMEOUT_S}s hard timeout"}), 504

        return jsonify(result)
    except ValueError as e:
        log.warning("ValueError in generate-daily-chart after %.1fs: %s", time.monotonic() - start, e)
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        log.error("Unhandled error in generate-daily-chart after %.1fs: %s", time.monotonic() - start, e)
        return jsonify({"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}), 500

@app.route("/generate-chart", methods=["POST"])
def generate_chart():
    start = time.monotonic()
    try:
        body = request.get_json(force=True)
        if body is None:
            return jsonify({"error": "Request body was empty or not valid JSON"}), 400

        # Run the actual work in the worker pool and enforce a hard ceiling on
        # it. This is the key safety net: if anything unexpected hangs (a lock
        # wait, a stalled render, a stuck upstream call fetch_bars' own 90s
        # deadline didn't anticipate), this request fails cleanly at
        # REQUEST_HARD_TIMEOUT_S instead of hanging indefinitely and making
        # the whole service look "offline" for every request queued behind it.
        future = _request_pool.submit(_build_chart_response, body, start)
        try:
            result = future.result(timeout=REQUEST_HARD_TIMEOUT_S)
        except FutureTimeoutError:
            log.error(
                "Hard timeout after %.1fs for %s -- abandoning (worker thread "
                "may still be running in the background, but this connection "
                "is released so it can't take the rest of the queue down with it)",
                time.monotonic() - start, body.get("symbol"),
            )
            return jsonify({
                "error": f"generate-chart exceeded the {REQUEST_HARD_TIMEOUT_S}s hard timeout"
            }), 504

        return jsonify(result)
    except ValueError as e:
        log.warning("ValueError after %.1fs: %s", time.monotonic() - start, e)
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        # Temporary: surface the real traceback in the response so it shows up in n8n
        # instead of a generic 500 page. Remove this except block once things are stable.
        log.error("Unhandled error after %.1fs: %s", time.monotonic() - start, e)
        return jsonify({
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()
        }), 500

def _build_full_day_response(body: dict) -> dict:
    symbol = (body.get("symbol") or "").strip().upper()
    trade_date = body["trade_date"]
    session_bars = get_full_day_bars(symbol, trade_date)
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "bars": serialize_bars(session_bars),
    }

@app.route("/full-day-bars", methods=["POST", "OPTIONS"])
def full_day_bars():
    """POST {symbol, trade_date} -> {symbol, trade_date, bars: [...]}, the
    same per-bar shape /generate-chart's "bars" field uses (o/h/l/c/v plus
    vwap/ema9/ema20/macd/macd_signal/macd_hist), but covering the WHOLE
    trading session for trade_date instead of just the ~WINDOW_BEFORE/
    WINDOW_AFTER minutes around one trade's entry/exit.

    Called on demand from the trade/practice/rewind pages' "Show full day"
    control -- never automatically -- so a page view alone never spends a
    Polygon call. When it IS clicked, it's routed through the same
    (symbol, trade_date) cache /generate-chart already uses (see
    _get_cached_raw_bars), so if this symbol+day was already fetched today
    -- by the original chart generation, by another trade on the same
    symbol+day, or by an earlier click of this same button -- this costs
    zero extra Polygon calls. Same reuse tactic polygon_client.py's
    _bars_cache uses for the backtester, just exposed live instead of only
    at analysis time."""
    if request.method == "OPTIONS":
        return "", 204

    start = time.monotonic()
    try:
        body = request.get_json(force=True, silent=True) or {}
        if not body.get("symbol") or not body.get("trade_date"):
            return jsonify({"error": "symbol and trade_date are required"}), 400

        future = _request_pool.submit(_build_full_day_response, body)
        try:
            result = future.result(timeout=REQUEST_HARD_TIMEOUT_S)
        except FutureTimeoutError:
            log.error("Hard timeout after %.1fs for full-day-bars %s", time.monotonic() - start, body.get("symbol"))
            return jsonify({"error": f"full-day-bars exceeded the {REQUEST_HARD_TIMEOUT_S}s hard timeout"}), 504

        return jsonify(result)
    except ValueError as e:
        log.warning("ValueError in full-day-bars after %.1fs: %s", time.monotonic() - start, e)
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        log.error("Unhandled error in full-day-bars after %.1fs: %s", time.monotonic() - start, e)
        return jsonify({"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}), 500

@app.route("/tick-data", methods=["POST", "OPTIONS"])
def tick_data():
    """Real trade prints for one short window (see the quiz's "Real ticks
    (from server)" mode in the dashboard) -- POST {symbol, start, end}
    where start/end are ISO 8601 timestamps (start inclusive, end
    exclusive), returns {"ticks": [{"t": <ISO8601>, "p": <price>}, ...]}
    oldest-first. Always returns 200 with an empty ticks list rather than
    an error for "no data in this window" -- the frontend treats a Polygon
    hiccup and a genuinely quiet window the same way (fall back to the
    simulated path), so there's no reason to distinguish them here except
    in the logs."""
    if request.method == "OPTIONS":
        return "", 204

    start = time.monotonic()
    body = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    raw_start, raw_end = body.get("start"), body.get("end")
    if not symbol or not raw_start or not raw_end:
        return jsonify({"error": "symbol, start, and end are required"}), 400

    try:
        start_dt = datetime.fromisoformat(str(raw_start).replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
    except ValueError:
        return jsonify({"error": "start/end must be ISO 8601 timestamps"}), 400
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=ET)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=ET)
    if end_dt <= start_dt:
        return jsonify({"error": "end must be after start"}), 400
    if (end_dt - start_dt) > timedelta(minutes=5):
        return jsonify({"error": "window too wide -- /tick-data is meant for single-bar (<=5min) windows"}), 400

    try:
        ticks = _fetch_trade_ticks(symbol, start_dt, end_dt)
        return jsonify({"ticks": ticks})
    except Exception as e:
        # Soft-fail: log the real cause but still return 200 + empty ticks
        # so the quiz's "Real ticks" mode falls back to simulated instead
        # of surfacing a raw error mid-question.
        log.warning("Tick fetch failed for %s [%s, %s) after %.1fs: %s", symbol, start_dt, end_dt, time.monotonic() - start, e)
        return jsonify({"ticks": [], "error": f"{type(e).__name__}: {e}"})

"""
chart_service.py
Flask microservice for the trade-review pipeline.

This file is now just the entrypoint: it builds the Flask app (see
config.py), imports every route module below (each registers its own
routes onto that app as a side effect of being imported), and re-exports
a few internal names that ai_routes.py / backtest_import_routes.py /
daily_sync.py still reach for via `from chart_service import ...` --
see the bottom of this file. The actual logic lives in:
  config.py            - Flask app, env-var config, cross-route auth helpers
  market_data.py        - Polygon bar-fetching/caching + indicators
  charts.py             - chart rendering + /generate-chart, /generate-daily-chart,
                           /full-day-bars, /tick-data
  float_routes.py       - /fetch-float, /fetch-float/bulk/*
  backtest_routes.py    - /backtest/* (config, start/status/cancel, history)
  strategies_routes.py  - /strategies/*, /backtest/history/<id>/save-strategy
  misc_routes.py        - /health, /gappers, scanner.py/scanner_enrich.py startup
The endpoint-by-endpoint request/response documentation below is unchanged.

POST /generate-chart
{
  "symbol": "AAPL",
  "trade_date": "2026-08-12",     # from the 'Trade Date' field
  "entry_time": "09:59:48",       # HH:MM:SS, from 'Entry Time'
  "exit_time": "10:14:12",        # HH:MM:SS, from 'Exit Time'
  "entry_price": 231.44,
  "exit_price": 232.10
}

Returns:
{
  "image_base64": None,            # PNG rendering is off by default (see include_image
                                    # below) -- null unless a caller opts in
  "indicators": {                 # ground-truth numbers, not read off pixels
    "vwap_at_entry": 231.02,
    "ema9_at_entry": 230.88,
    "ema20_at_entry": 230.41,
    "ema200_at_entry": 228.77,
    "macd_at_entry": 0.14,
    "macd_signal_at_entry": 0.09,
    "macd_hist_at_entry": 0.05,
    "macd_hist_prior_bar": 0.03,
    "entry_vs_vwap": "above",
    "entry_vs_ema9": "above",
    "entry_vs_ema20": "above",
    "entry_vs_ema200": "above"
  },
  "bars": [                       # display-window OHLCV + indicators, one
                                   # object per minute, for the dashboard's
                                   # client-side interactive chart (see
                                   # dashboard/README.md). NOT the full
                                   # lookback-padded series -- same window
                                   # that's rendered into image_base64.
    {
      "t": "2026-08-12T08:30:00",
      "o": 231.10, "h": 231.30, "l": 231.05, "c": 231.20, "v": 4231,
      "vwap": 231.02, "ema9": 230.88, "ema20": 230.41, "ema200": 228.77,
      "macd": 0.14, "macd_signal": 0.09, "macd_hist": 0.05
    },
    ...
  ]
}

Data source: Polygon.io (consolidated tape across all US exchanges — not just
IEX, which matters for thinly-traded small caps where the IEX-only slice of
volume can badly distort VWAP).

Indicator accuracy notes:
  - VWAP resets at the 9:30 ET session open every trading day (a real
    "anchored" session VWAP), not from an arbitrary point mid-window.
  - EMA9 / EMA20 / EMA200 / MACD are computed over a multi-day lookback so
    they have a proper warm-up period before the display window, instead of
    being seeded artificially at the first bar of the chart.

indicators also now carries (best-effort -- see VOLUME_FLOAT_STATS below):
  "volume_on_entry_day": 8213400,   # that trading day's TOTAL volume (not
                                     # just the minute-window shown on chart)
  "avg_volume_30d": 2140335.2,      # mean daily volume over the ~30 trading
                                     # days strictly before trade_date
  "relative_volume": 3.84,          # volume_on_entry_day / avg_volume_30d
  "float_shares": None,             # always null out of /generate-chart now --
                                     # float is no longer fetched automatically
                                     # per trade (see POST /fetch-float below)
  "avg_volume_tag": "avgvol_1m_5m", # bucketed tags, see classify_* below --
  "rvol_tag": "rvol_2x_5x",         # these are what the publish step copies
  "float_tag": "float_unknown"      # onto data/trades.json for journal filters --
                                     # stays "float_unknown" until /fetch-float
                                     # is called for this trade

Any of the volume/avg_volume/rvol fields/tags can be null if Polygon's
daily-aggs calls fail or the plan doesn't include them -- this never fails
the whole /generate-chart call.

POST /fetch-float
{
  "trade_id": "AAPL-20260812-095948",
  "symbol": "AAPL"
}
On-demand float lookup, requires an Authorization: Bearer <supabase JWT>
header. Hits Polygon's ticker reference endpoint (share_class_shares_
outstanding, a commonly-used float PROXY, not an exact tradable-float
figure -- same field /generate-chart used to fetch automatically), but
only when the user asks for THIS symbol by clicking "Get float" on the
trade detail page, and only ever once per symbol overall (see
float_shares_store.py). Also merges the result into that trade's stored
indicators/float_tag in Supabase so it's there next time the trade is
opened. Returns {"float_shares": 18500000, "float_tag": "float_low_10m_20m"}.

POST /generate-daily-chart
{
  "symbol": "AAPL",
  "trade_date": "2026-08-12",     # only days STRICTLY BEFORE this date are
                                   # returned -- what the trader could have
                                   # actually seen going into the trade
  "lookback_days": 40             # optional, default 40 trading days
}

Returns:
{
  "symbol": "AAPL",
  "trade_date": "2026-08-12",
  "bars": [ { "t": "2026-06-10", "o": ..., "h": ..., "l": ..., "c": ..., "v": ... }, ... ],
  "computed_levels": {              # cheap, no-LLM pivot-cluster S/R --
    "support": [ { "price": 228.40, "touches": 3 }, ... ],   # a fallback/
    "resistance": [ { "price": 235.90, "touches": 2 }, ... ] # sanity check
  },
  "image_base64": None               # daily candlestick+volume PNG rendering is off
                                      # by default (see include_image below) -- null
                                      # unless a caller opts in
}

This endpoint is never called by the automatic daily pipeline -- it only
exists for the trade site's optional "Support & Resistance (AI)" button, so
reviewing a trade never spends an extra Polygon/LLM call unless you
explicitly ask for one.

POST /full-day-bars
{
  "symbol": "AAPL",
  "trade_date": "2026-08-12"
}

Returns:
{
  "symbol": "AAPL",
  "trade_date": "2026-08-12",
  "bars": [ ... ]              # same per-bar shape as /generate-chart's
                                # "bars", but for the WHOLE session that
                                # day (pre-market through after-hours),
                                # not just the narrow entry/exit window
}

On-demand only -- the trade/practice/rewind pages' "Show full day" control
calls this when someone actually wants more chart context than what got
stored for a trade. Shares its Polygon fetch + cache with /generate-chart
(keyed by symbol+trade_date, same as polygon_client.py's cache for the
backtester), so calling this for a symbol+day already charted today --
this trade's own chart, a different trade on the same symbol+day, or an
earlier click of this same button -- is free.

POST /tick-data
{
  "symbol": "AAPL",
  "start": "2026-08-14T09:37:00-04:00",   # ISO 8601, inclusive
  "end": "2026-08-14T09:38:00-04:00"      # ISO 8601, exclusive -- capped to a 5min window
}

Returns:
{
  "ticks": [
    { "t": "2026-08-14T13:37:01.482341+00:00", "p": 4.5231 },
    ...
  ]
}

Real executed trade prints (Polygon's v3 trades endpoint), not the
1-minute aggregate bars the rest of this service is built on -- powers the
quiz's optional "Real ticks (from server)" playback mode (dashboard/
quiz.js), as an alternative to its offline synthesized second-by-second
path. Always returns 200, even on a Polygon failure or a genuinely quiet
window -- `{"ticks": []}` either way, since the frontend treats "no data"
and "couldn't reach the server" the same (falls back to simulated). Same
CORS/rate-limiter/caching treatment as the rest of this file.

Env vars:
  POLYGON_API_KEY          - your Polygon.io API key
  CHART_WINDOW_BEFORE_MIN  - minutes of context before entry (default 120)
  CHART_WINDOW_AFTER_MIN   - minutes of context after exit (default 120)
  CHART_LOOKBACK_DAYS      - calendar days of prior bars fetched purely to
                              warm up EMA/MACD (default 5, not displayed)
  POLYGON_BATCH_SIZE       - Polygon calls allowed per batch before the
                              service pauses (default 5, matching free tier)
  POLYGON_BATCH_WINDOW_S   - seconds to wait between batches (default 65,
                              i.e. Polygon's 60s/5-call window plus buffer)
  POLYGON_BATCH_MIN_GAP_S  - minimum stagger between calls within one batch
                              (default 2.0)
  ENABLE_VOLUME_FLOAT_STATS - "true"/"false" (default "true"). Each chart
                              generation costs 1 extra Polygon call for
                              float (cached indefinitely per symbol) and 1
                              for the 30-day daily-volume window (cached per
                              symbol+date) -- set to "false" to skip both
                              and keep /generate-chart to its original
                              single Polygon call, if you're on the free
                              tier and the extra pacing wait is too slow.
  VOLUME_STATS_LOOKBACK_DAYS - trading days of daily volume averaged for
                              avg_volume_30d / relative_volume (default 30)
  TICK_DATA_MAX_TRADES     - cap on prints returned by one /tick-data call
                              (default 2000), so a busy minute on a liquid
                              ticker can't blow up the response payload
"""

import os

from config import app

# Each import below registers that module's routes onto `app` purely as an
# import-time side effect -- see each module's own docstring for what it
# owns. Order matters only in that market_data must be import-ready before
# misc_routes (which passes the market_data module itself into
# scanner_enrich.start() -- see misc_routes.py) runs.
import market_data
import charts
import float_routes
import backtest_routes
import strategies_routes
import misc_routes

# ai_routes.py / backtest_import_routes.py / daily_sync.py still do
# `from chart_service import _build_chart_response` etc. (lazily, inside
# their own route functions) -- keep those names live here so that keeps
# working without having to touch those three files.
from charts import _build_chart_response, _build_daily_chart_response
from config import SR_LOOKBACK_DAYS_DEFAULT  # noqa: F401 -- re-exported for ai_routes.py

from ai_routes import bp as ai_bp
app.register_blueprint(ai_bp)

from import_routes import bp as import_bp
app.register_blueprint(import_bp)

from backtest_import_routes import bp as backtest_import_bp
app.register_blueprint(backtest_import_bp)

from daily_sync import bp as daily_sync_bp
app.register_blueprint(daily_sync_bp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), threaded=True)

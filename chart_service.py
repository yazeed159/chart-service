"""
chart_service.py
Flask microservice for the trade-review pipeline.

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
    "macd_at_entry": 0.14,
    "macd_signal_at_entry": 0.09,
    "macd_hist_at_entry": 0.05,
    "macd_hist_prior_bar": 0.03,
    "entry_vs_vwap": "above",
    "entry_vs_ema9": "above",
    "entry_vs_ema20": "above"
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
      "vwap": 231.02, "ema9": 230.88, "ema20": 230.41,
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
  - EMA9 / EMA20 / MACD are computed over a multi-day lookback so they have
    a proper warm-up period before the display window, instead of being
    seeded artificially at the first bar of the chart.

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
import io
import time
import base64
import threading
import traceback
import logging
from datetime import datetime, timedelta, date, time as dtime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

import requests
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # headless rendering — must be set before importing mplfinance/pyplot
import mplfinance as mpf
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import MaxNLocator, AutoMinorLocator
from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("chart_service")

app = Flask(__name__)

# matplotlib/mplfinance keep process-global state (current figure/axes) that is
# NOT thread-safe. app.run(threaded=True) below hands each request its own
# thread, so two requests rendering at the same time can corrupt or deadlock on
# that shared state -- which then wedges the *whole* process, not just the one
# request (this is what turns "one slow trade" into "every request after it
# times out with connection refused/aborted"). Serialize anything that touches
# matplotlib through this lock so only one render runs at a time.
RENDER_LOCK = threading.Lock()

# Hard ceiling on total request handling time, independent of fetch_bars'
# internal network deadline. If ANYTHING hangs (a lock wait, a matplotlib
# stall, etc.) this guarantees the request fails with a clean error instead of
# hanging indefinitely and taking the server down for every request behind it.
# The n8n workflow already paces calls to this endpoint one-at-a-time, 13s
# apart (see the Generate Chart node's batching options) -- so this only
# needs to cover an occasional 429 backoff plus render time, not a long
# queue wait. Keep the n8n node's own "timeout" option comfortably above
# this value (it was 45000ms, which is too tight -- see chat).
REQUEST_HARD_TIMEOUT_S = 60
_request_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="chart-req")

POLYGON_API_KEY = os.environ["POLYGON_API_KEY"]
WINDOW_BEFORE = int(os.environ.get("CHART_WINDOW_BEFORE_MIN", 120))
WINDOW_AFTER = int(os.environ.get("CHART_WINDOW_AFTER_MIN", 120))
LOOKBACK_DAYS = int(os.environ.get("CHART_LOOKBACK_DAYS", 5))
ENABLE_VOLUME_FLOAT_STATS = os.environ.get("ENABLE_VOLUME_FLOAT_STATS", "true").lower() not in ("false", "0", "no")
VOLUME_STATS_LOOKBACK_DAYS = int(os.environ.get("VOLUME_STATS_LOOKBACK_DAYS", 30))
SR_LOOKBACK_DAYS_DEFAULT = int(os.environ.get("SR_LOOKBACK_DAYS_DEFAULT", 40))

# Polygon's free tier allows 5 API calls/minute. The n8n workflow itself
# already sends requests to this endpoint one-at-a-time, spaced 13s apart
# (the Generate Chart node's batching option), which is already under the
# 12s-per-call minimum the free tier needs -- so in normal operation this
# limiter should rarely if ever have to wait. It exists as a safety net for
# anything that doesn't go through that pacing (manual testing, retries,
# future workflow changes with a larger batch). BATCH_SIZE=1 makes it a
# plain "wait at least WINDOW_S since the last call" limiter rather than a
# 5-then-wait-a-minute one, matching the workflow's actual call pattern
# instead of fighting it.
POLYGON_BATCH_SIZE = int(os.environ.get("POLYGON_BATCH_SIZE", 1))
POLYGON_BATCH_WINDOW_S = float(os.environ.get("POLYGON_BATCH_WINDOW_S", 13))
POLYGON_BATCH_MIN_GAP_S = float(os.environ.get("POLYGON_BATCH_MIN_GAP_S", 2.0))  # only matters if BATCH_SIZE > 1

ET = ZoneInfo("America/New_York")
SESSION_VWAP_START = dtime(4, 0)  # session VWAP resets here each day — pre-market open, not 9:30 regular open
REGULAR_SESSION_START = dtime(9, 30)  # regular-hours open — used to line up locally-resampled
REGULAR_SESSION_END = dtime(16, 0)    # daily bars with what Polygon's own day-aggregate endpoint returns

# compute_volume_float_stats() used to make its own Polygon call for daily
# bars (a separate /range/1/day request, ~58 calendar days back). That's
# now derived instead by resampling the 1-minute bars this service already
# fetches for the chart -- see _resample_daily_from_minute_bars() -- so the
# minute-bar fetch itself needs to reach back far enough to cover it. Only
# widen the window when the volume/float stats are actually enabled;
# otherwise stick to the narrow chart-only lookback.
def _volume_stats_calendar_lookback_days() -> int:
    # Same padding compute_volume_float_stats always used: 1.6x the trading-day
    # lookback plus a week of slack to comfortably absorb weekends/holidays.
    return int(VOLUME_STATS_LOOKBACK_DAYS * 1.6) + 10


class _PolygonBatchLimiter:
    """Process-wide '5 calls, then wait, then next 5' pacing matching
    Polygon's free-tier limit, instead of continuous spacing. Every call to
    wait_turn() either passes straight through (still under the batch cap,
    respecting the small in-batch stagger) or blocks until the next batch
    window opens -- so a burst of requests naturally gets throttled into
    batches no matter how many arrive at once or how they're spaced by n8n."""

    def __init__(self, batch_size: int, window_s: float, min_gap_s: float):
        self._batch_size = batch_size
        self._window_s = window_s
        self._min_gap_s = min_gap_s
        self._lock = threading.Lock()
        self._count_in_batch = 0
        self._window_started_at = None
        self._last_call_at = None

    def wait_turn(self):
        # IMPORTANT: any time.sleep() here must happen OUTSIDE the lock.
        # This used to sleep while holding self._lock, which meant every
        # other thread waiting for a Polygon turn (e.g. the other 7 workers
        # in a bulk backtest-journal send) blocked on the lock itself for
        # the full sleep duration, on top of the batch pacing they were
        # already waiting for -- turning what should be "one call every
        # 13s" into a much longer, effectively fully-serial queue across
        # every concurrent /generate-chart request.
        while True:
            sleep_for = 0.0
            with self._lock:
                now = time.monotonic()
                if self._window_started_at is None:
                    self._window_started_at = now

                if self._count_in_batch >= self._batch_size:
                    remaining = self._window_s - (now - self._window_started_at)
                    if remaining > 0:
                        sleep_for = remaining
                    else:
                        self._count_in_batch = 0
                        self._window_started_at = now
                        self._last_call_at = None

                if sleep_for == 0.0 and self._last_call_at is not None:
                    gap_remaining = self._min_gap_s - (now - self._last_call_at)
                    if gap_remaining > 0:
                        sleep_for = gap_remaining

                if sleep_for == 0.0:
                    self._count_in_batch += 1
                    self._last_call_at = now
                    return

            log.info("Polygon batch limiter: waiting %.1fs for next turn", sleep_for)
            time.sleep(sleep_for)


_polygon_limiter = _PolygonBatchLimiter(POLYGON_BATCH_SIZE, POLYGON_BATCH_WINDOW_S, POLYGON_BATCH_MIN_GAP_S)

# The raw bar fetch depends only on (symbol, trade_date) -- NOT on the
# specific entry/exit times -- so multiple trades on the same symbol/day
# (common when reviewing a batch from one session) would otherwise each pay
# for an identical Polygon call. Cache it. Small process-lifetime cache, no
# TTL needed since past-day bars don't change; capped size with FIFO eviction
# so it can't grow unbounded across a long-running n8n batch.
_BARS_CACHE_MAX = 200
_bars_cache = {}
_bars_cache_lock = threading.Lock()

# Float (share count) practically never changes -- cache it per symbol for
# the life of the process, no eviction needed at any realistic symbol count.
_float_cache = {}
_float_cache_lock = threading.Lock()

# Daily bars are used both for the 30d-avg-volume/rvol stat on the normal
# /generate-chart call AND for the optional /generate-daily-chart
# support/resistance button. Same FIFO-capped-cache pattern as _bars_cache.
_DAILY_BARS_CACHE_MAX = 200
_daily_bars_cache = {}
_daily_bars_cache_lock = threading.Lock()


def _get_cached_raw_bars(symbol: str, trade_date_obj: date) -> pd.DataFrame:
    """Shared entry point for anything that needs a symbol's raw minute bars
    for one trading day -- fetch_bars() (below, backs /generate-chart) AND
    get_full_day_bars() (backs /full-day-bars). Both funnel through this
    same (symbol, trade_date) cache, so two trades on the same symbol+day --
    or a trade's normal chart plus a later "show full day" click on it, or
    that same click on a DIFFERENT trade sharing the symbol+day -- pay for
    exactly one Polygon call between them. This is the identical
    cache-by-(symbol, date) tactic polygon_client.py's _bars_cache uses for
    the backtester, just shared across both entry points here."""
    cache_key = (symbol, trade_date_obj)
    with _bars_cache_lock:
        cached = _bars_cache.get(cache_key)
    if cached is not None:
        log.info("Bars cache hit for %s %s -- skipping Polygon call", symbol, trade_date_obj)
        return cached

    df = _fetch_raw_bars_from_polygon(symbol, trade_date_obj)
    with _bars_cache_lock:
        if len(_bars_cache) >= _BARS_CACHE_MAX:
            _bars_cache.pop(next(iter(_bars_cache)))  # evict oldest (dict insertion order)
        _bars_cache[cache_key] = df
    return df


def fetch_bars(symbol: str, trade_date: str, entry_dt: datetime, exit_dt: datetime):
    """
    Pull 1-minute bars from Polygon covering [trade_date - LOOKBACK_DAYS, trade_date].
    The extra lookback is only there to warm up EMA/MACD — it's not shown on
    the chart. Returns (full_df, display_mask).
    """
    trade_date_obj = datetime.strptime(trade_date, "%Y-%m-%d").date()
    df = _get_cached_raw_bars(symbol, trade_date_obj)

    window_start = entry_dt - timedelta(minutes=WINDOW_BEFORE)
    window_end = exit_dt + timedelta(minutes=WINDOW_AFTER)
    display_mask = (df.index >= window_start) & (df.index <= window_end)

    if not display_mask.any():
        raise ValueError(f"No bars in display window {window_start}–{window_end} for {symbol} (thin small-cap volume can leave gaps — try widening CHART_WINDOW_BEFORE_MIN)")

    return df, display_mask


def get_full_day_bars(symbol: str, trade_date: str) -> pd.DataFrame:
    """Every bar Polygon has for trade_date itself (pre-market through
    after-hours), with VWAP/EMA9/EMA20/MACD computed -- this is what backs
    the trade/practice/rewind pages' "show full day" zoom-out (see
    /full-day-bars below). Goes through the exact same (symbol, trade_date)
    cache fetch_bars() uses above, so asking for a symbol+day that's
    already been charted today -- whether that's this same trade's own
    /generate-chart call earlier, a second trade on the same symbol+day, or
    an earlier /full-day-bars click on either -- costs zero extra Polygon
    calls."""
    trade_date_obj = datetime.strptime(trade_date, "%Y-%m-%d").date()
    raw = _get_cached_raw_bars(symbol, trade_date_obj)
    with_indicators = compute_indicators(raw)
    session_only = with_indicators[with_indicators.index.date == trade_date_obj]
    if session_only.empty:
        raise ValueError(f"No bars found for {symbol} on {trade_date} (holiday/weekend, or check the ticker)")
    return session_only


def _minute_bar_lookback_days() -> int:
    """How far back the minute-bar fetch reaches. Normally just LOOKBACK_DAYS
    (EMA/MACD warm-up). When volume/float stats are enabled, widened to cover
    VOLUME_STATS_LOOKBACK_DAYS as well, so _resample_daily_from_minute_bars()
    has enough history and compute_volume_float_stats never has to make its
    own separate Polygon call for daily bars."""
    if ENABLE_VOLUME_FLOAT_STATS:
        return max(LOOKBACK_DAYS, _volume_stats_calendar_lookback_days())
    return LOOKBACK_DAYS


def _fetch_raw_bars_from_polygon(symbol: str, trade_date_obj: date) -> pd.DataFrame:
    fetch_start_date = trade_date_obj - timedelta(days=_minute_bar_lookback_days())

    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/minute/{fetch_start_date}/{trade_date_obj}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": POLYGON_API_KEY}

    all_bars = []
    next_url = url
    page_count = 0
    max_pages = 10
    # Hard wall-clock cap across ALL pages combined, including limiter waits.
    # Sized to fit comfortably under REQUEST_HARD_TIMEOUT_S (60s) with room
    # left over for rendering.
    fetch_deadline = time.monotonic() + 45
    while next_url:
        page_count += 1
        if page_count > max_pages:
            raise ValueError(f"Polygon pagination for {symbol} exceeded {max_pages} pages -- aborting instead of hanging.")
        if time.monotonic() > fetch_deadline:
            raise ValueError(f"Fetching bars for {symbol} took longer than 45s across {page_count} page(s) -- aborting instead of hanging.")

        # Wait for our turn under the batch limiter BEFORE calling -- this is
        # what actually enforces "5 calls, then wait ~60s, then next 5"
        # across every request hitting this process, regardless of how n8n
        # schedules them (parallel or sequential).
        _polygon_limiter.wait_turn()

        # Still retry on 429 for the rare case another process/instance is
        # also burning the shared free-tier quota -- but with a tighter
        # budget now that proactive pacing should make this the exception,
        # not the norm. IMPORTANT: this stays well under n8n's real HTTP
        # Request timeout. n8n's "timeout" option is unreliable (known n8n
        # bug -- it often silently falls back to a hardcoded ~300s
        # regardless of what's configured), so this service must never let a
        # single request run anywhere near that long. If Polygon's
        # Retry-After is large (daily quota exhausted, not just per-minute
        # throttling), we fail fast instead of sleeping through it.
        max_attempts = 3
        max_single_wait_s = 15
        max_total_wait_s = 30
        total_waited = 0.0
        for attempt in range(1, max_attempts + 1):
            resp = requests.get(next_url, params=params if next_url == url else None, timeout=15)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait_s = min(float(retry_after), max_single_wait_s) if retry_after else min(2 ** attempt * 3, max_single_wait_s)
                log.warning(
                    "Polygon 429 for %s (attempt %d/%d, Retry-After=%r) despite pacing -- waiting %.1fs",
                    symbol, attempt, max_attempts, retry_after, wait_s
                )
                if attempt == max_attempts or total_waited + wait_s > max_total_wait_s:
                    raise ValueError(
                        f"Polygon rate-limited us repeatedly for {symbol} (429, Retry-After={retry_after!r}) "
                        f"even with the {POLYGON_BATCH_SIZE}-per-{POLYGON_BATCH_WINDOW_S:.0f}s batch limiter. "
                        f"This likely means another process is sharing the same free-tier key/quota, or it's a "
                        f"daily/monthly quota rather than per-minute throttling -- consider lowering "
                        f"POLYGON_BATCH_SIZE, raising POLYGON_BATCH_WINDOW_S, or upgrading the Polygon plan."
                    )
                time.sleep(wait_s)
                total_waited += wait_s
                continue
            resp.raise_for_status()
            break

        payload = resp.json()
        if payload.get("status") not in ("OK", "DELAYED") and not payload.get("results"):
            raise ValueError(f"Polygon returned status={payload.get('status')} for {symbol}: {payload.get('error') or payload.get('message')}")
        n_results = len(payload.get("results") or [])
        all_bars.extend(payload.get("results") or [])
        next_url = payload.get("next_url")
        if next_url:
            log.info("Polygon page %d for %s: %d bars, more pages pending", page_count, symbol, n_results)
            next_url = f"{next_url}&apiKey={POLYGON_API_KEY}"

    if not all_bars:
        raise ValueError(f"No bars returned for {symbol} between {fetch_start_date} and {trade_date_obj} (check the ticker is correct and Polygon's plan covers this history)")

    df = pd.DataFrame(all_bars)
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(ET)
    df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume", "vw": "vwap_bar"})
    df = df.set_index("t")[["Open", "High", "Low", "Close", "Volume", "vwap_bar"]].sort_index()
    return _fill_intraday_gaps(df)


def _fill_intraday_gaps(df: pd.DataFrame) -> pd.DataFrame:
    """Polygon only returns a bar for a minute if a trade actually printed
    in it -- thin/illiquid tickers (especially pre/post-market on small
    caps) can leave real gaps of several minutes with no bar at all. Left
    as-is, that silently compresses the chart: a candlestick chart plots
    bar-by-bar, not on a true time axis, so 20 real minutes with only 3
    prints renders as if only 3 minutes passed -- which is why widening
    CHART_WINDOW_BEFORE_MIN/AFTER_MIN alone didn't fix a chart that looked
    like it only covered ~10 minutes. Reindex each trading day present to a
    continuous 1-minute grid and forward-fill the missing minutes as flat,
    zero-volume bars at the last known price, so the configured window
    actually renders as that much time."""
    if df.empty:
        return df
    filled = []
    for _, day_df in df.groupby(df.index.date):
        full_idx = pd.date_range(day_df.index.min(), day_df.index.max(), freq="1min", tz=day_df.index.tz)
        day_df = day_df.reindex(full_idx)
        day_df["Volume"] = day_df["Volume"].fillna(0)
        day_df["Close"] = day_df["Close"].ffill()
        day_df["Open"] = day_df["Open"].fillna(day_df["Close"])
        day_df["High"] = day_df["High"].fillna(day_df["Close"])
        day_df["Low"] = day_df["Low"].fillna(day_df["Close"])
        day_df["vwap_bar"] = day_df["vwap_bar"].fillna(day_df["Close"])
        filled.append(day_df)
    return pd.concat(filled).sort_index()


def _fetch_daily_bars_from_polygon(symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
    """Daily OHLCV bars for [start_date, end_date] inclusive. Cached per
    (symbol, start_date, end_date) -- callers should ask for a window wide
    enough to cover what they need rather than re-requesting slightly
    different ranges, so the cache actually gets reused."""
    cache_key = (symbol, start_date, end_date)
    with _daily_bars_cache_lock:
        cached = _daily_bars_cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start_date}/{end_date}"
    params = {"adjusted": "true", "sort": "asc", "limit": 5000, "apiKey": POLYGON_API_KEY}

    _polygon_limiter.wait_turn()
    resp = requests.get(url, params=params, timeout=15)
    if resp.status_code == 429:
        raise ValueError(f"Polygon rate-limited the daily-bars call for {symbol}")
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") not in ("OK", "DELAYED") and not payload.get("results"):
        raise ValueError(f"Polygon returned status={payload.get('status')} for daily bars of {symbol}: {payload.get('error') or payload.get('message')}")

    results = payload.get("results") or []
    df = pd.DataFrame(results)
    if df.empty:
        df = pd.DataFrame(columns=["t", "Open", "High", "Low", "Close", "Volume"]).set_index("t")
    else:
        df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(ET).dt.date
        df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
        df = df.set_index("t")[["Open", "High", "Low", "Close", "Volume"]].sort_index()

    with _daily_bars_cache_lock:
        if len(_daily_bars_cache) >= _DAILY_BARS_CACHE_MAX:
            _daily_bars_cache.pop(next(iter(_daily_bars_cache)))
        _daily_bars_cache[cache_key] = df
    return df


def _fetch_float_shares(symbol: str):
    """share_class_shares_outstanding from Polygon's ticker reference data --
    a commonly-used proxy for float in retail scanners. Not the same as a
    precise tradable float (which would need to exclude insider/locked-up
    shares a data vendor like this doesn't expose), so it's presented to the
    user as an approximation. Raises on a genuine live-Polygon failure --
    callers (currently only POST /fetch-float; this used to also be called
    from compute_volume_float_stats on every /generate-chart, but no
    longer is -- see that function's docstring) decide how to handle that.

    Lookup order: in-memory cache (this process) -> symbol_float_shares
    table in Supabase (persists across restarts/redeploys, shared across
    every user's journal) -> Polygon, only on a genuine first-ever miss.
    See float_shares_store.py's docstring for why this makes float a true
    one-time-per-symbol Polygon call instead of a per-restart one."""
    with _float_cache_lock:
        if symbol in _float_cache:
            return _float_cache[symbol]

    try:
        stored = float_shares_store.get_float_shares(symbol)
    except Exception as e:
        log.warning("float_shares_store lookup failed for %s -- falling back to Polygon: %s", symbol, e)
        stored = None
    if stored is not None:
        shares = stored["shares"]
        with _float_cache_lock:
            _float_cache[symbol] = shares
        return shares

    url = f"https://api.polygon.io/v3/reference/tickers/{symbol}"
    params = {"apiKey": POLYGON_API_KEY}
    _polygon_limiter.wait_turn()
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    results = payload.get("results") or {}
    shares = results.get("share_class_shares_outstanding") or results.get("weighted_shares_outstanding")
    shares = int(shares) if shares else None

    try:
        float_shares_store.save_float_shares(symbol, shares)
    except Exception as e:
        # Non-fatal: worst case, this symbol just costs another live
        # Polygon call next time instead of being served from the table.
        log.warning("float_shares_store save failed for %s (non-fatal): %s", symbol, e)

    with _float_cache_lock:
        _float_cache[symbol] = shares
    return shares


# Real trade-print cache, keyed by (symbol, start_iso, end_iso) -- windows
# are short (usually well under a minute, driven by the quiz's checkpoint/
# entry bars) and get replayed often within one quiz session, so this
# avoids re-hitting Polygon every time someone re-plays or reopens a
# question. Same small-FIFO-cache shape as _bars_cache/_float_cache.
_TICKS_CACHE_MAX = 500
_ticks_cache = {}
_ticks_cache_lock = threading.Lock()

# One request to /tick-data can, at worst, ask for a full 60s bar on a
# heavily-traded ticker -- cap how many individual prints we'll pull back
# and hand to the browser so neither Polygon pagination nor the response
# payload can run away on us.
TICK_DATA_MAX_TRADES = int(os.environ.get("TICK_DATA_MAX_TRADES", 2000))


def _fetch_trade_ticks(symbol: str, start_dt: datetime, end_dt: datetime) -> list:
    """Real executed trade prints for [start_dt, end_dt) from Polygon's v3
    trades endpoint -- actual prints, not the 1-minute aggregate bars the
    rest of this service is built on. Returns a list of {"t": <ISO8601>,
    "p": <price>} dicts, oldest first, capped at TICK_DATA_MAX_TRADES.
    Raises on a hard Polygon failure -- the /tick-data route below is what
    turns that into a soft `{"ticks": []}` so the quiz can fall back to its
    simulated path instead of failing outright."""
    cache_key = (symbol, start_dt.isoformat(), end_dt.isoformat())
    with _ticks_cache_lock:
        cached = _ticks_cache.get(cache_key)
    if cached is not None:
        return cached

    start_ns = int(start_dt.timestamp() * 1_000_000_000)
    end_ns = int(end_dt.timestamp() * 1_000_000_000)

    url = f"https://api.polygon.io/v3/trades/{symbol}"
    params = {
        "timestamp.gte": start_ns,
        "timestamp.lt": end_ns,
        "order": "asc",
        "sort": "timestamp",
        "limit": 50000,
        "apiKey": POLYGON_API_KEY,
    }

    all_trades = []
    next_url = url
    page_count = 0
    max_pages = 3  # a single sub-minute window should never need more than this
    fetch_deadline = time.monotonic() + 20

    while next_url and len(all_trades) < TICK_DATA_MAX_TRADES:
        page_count += 1
        if page_count > max_pages:
            log.warning("Tick fetch for %s hit the %d-page cap -- truncating", symbol, max_pages)
            break
        if time.monotonic() > fetch_deadline:
            log.warning("Tick fetch for %s took longer than 20s -- truncating", symbol)
            break

        _polygon_limiter.wait_turn()

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            resp = requests.get(next_url, params=params if next_url == url else None, timeout=15)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait_s = min(float(retry_after), 15) if retry_after else min(2 ** attempt * 3, 15)
                log.warning("Polygon 429 fetching trades for %s (attempt %d/%d) -- waiting %.1fs", symbol, attempt, max_attempts, wait_s)
                if attempt == max_attempts:
                    resp.raise_for_status()
                time.sleep(wait_s)
                continue
            resp.raise_for_status()
            break

        payload = resp.json()
        results = payload.get("results") or []
        all_trades.extend(results)
        next_url = payload.get("next_url")
        if next_url:
            next_url = f"{next_url}&apiKey={POLYGON_API_KEY}"

    ticks = [
        {"t": pd.Timestamp(t["sip_timestamp"], unit="ns", tz="UTC").isoformat(), "p": t["price"]}
        for t in all_trades[:TICK_DATA_MAX_TRADES]
        if t.get("price")
    ]

    with _ticks_cache_lock:
        if len(_ticks_cache) >= _TICKS_CACHE_MAX:
            _ticks_cache.pop(next(iter(_ticks_cache)))  # evict oldest (dict insertion order)
        _ticks_cache[cache_key] = ticks
    return ticks


def classify_float(shares) -> str:
    if not shares:
        return "float_unknown"
    if shares < 10_000_000:
        return "float_micro_under_10m"
    if shares < 20_000_000:
        return "float_low_10m_20m"
    if shares < 50_000_000:
        return "float_mid_20m_50m"
    if shares < 200_000_000:
        return "float_large_50m_200m"
    return "float_mega_200m_plus"


def classify_avg_volume(avg_vol) -> str:
    if not avg_vol:
        return "avgvol_unknown"
    if avg_vol < 500_000:
        return "avgvol_under_500k"
    if avg_vol < 1_000_000:
        return "avgvol_500k_1m"
    if avg_vol < 5_000_000:
        return "avgvol_1m_5m"
    if avg_vol < 20_000_000:
        return "avgvol_5m_20m"
    return "avgvol_20m_plus"


def classify_relative_volume(rvol) -> str:
    if rvol is None:
        return "rvol_unknown"
    if rvol < 1:
        return "rvol_under_1x"
    if rvol < 2:
        return "rvol_1x_2x"
    if rvol < 5:
        return "rvol_2x_5x"
    if rvol < 10:
        return "rvol_5x_10x"
    return "rvol_10x_plus"


def _resample_daily_from_minute_bars(full_bars: pd.DataFrame) -> pd.DataFrame:
    """Build daily OHLCV bars by resampling the 1-minute bars this service
    already fetched for the chart, instead of a second Polygon call to
    /range/1/day. Restricted to the 09:30-16:00 ET regular session (the
    minute-bar fetch spans pre-market through after-hours -- see
    get_full_day_bars' docstring) so the result lines up with what Polygon's
    own regular-session daily aggregate would return. _fill_intraday_gaps
    already zero-fills any missing minutes, so summing Volume here doesn't
    double-count or fabricate volume on thin/gappy days.

    Index is plain datetime.date objects (not Timestamps), matching what
    _fetch_daily_bars_from_polygon used to return, so callers can keep
    comparing/indexing against trade_date_obj (a date) unchanged."""
    if full_bars.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    session_time = full_bars.index.time
    regular = full_bars[(session_time >= REGULAR_SESSION_START) & (session_time < REGULAR_SESSION_END)]
    if regular.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    daily = regular.groupby(regular.index.date).agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
    )
    return daily


def compute_volume_float_stats(symbol: str, trade_date_obj: date, full_bars: pd.DataFrame) -> dict:
    """Best-effort. Derives daily bars from the minute bars already fetched
    for this chart (see _resample_daily_from_minute_bars) -- no separate
    Polygon call -- covering the entry day itself (for volume_on_entry_day)
    and VOLUME_STATS_LOOKBACK_DAYS prior trading days for the 30d average,
    computed STRICTLY BEFORE trade_date (excluding entry day so rvol doesn't
    measure a day against itself).

    Does NOT fetch float shares anymore -- that used to happen right here,
    on every single /generate-chart call, via a call to _fetch_float_shares
    (cached per-symbol, but still a live Polygon hit the first time any
    trade on a given symbol was published). Float is now strictly on-demand:
    see POST /fetch-float, wired to a "Get float" button on the trade
    detail page, which calls _fetch_float_shares itself only when a user
    actually wants that number for that symbol. So float_shares/float_tag
    below always come back null/"float_unknown" out of this function --
    they get filled in later, per trade, by /fetch-float.

    Any failure here degrades to nulls/unknown tags rather than raising --
    this must never take down a /generate-chart call over a secondary stat."""
    empty = {
        "volume_on_entry_day": None, "avg_volume_30d": None, "relative_volume": None,
        "float_shares": None, "avg_volume_tag": "avgvol_unknown",
        "rvol_tag": "rvol_unknown", "float_tag": "float_unknown",
    }
    if not ENABLE_VOLUME_FLOAT_STATS:
        return empty

    try:
        daily = _resample_daily_from_minute_bars(full_bars)
        if daily.empty:
            return empty

        prior = daily.loc[daily.index < trade_date_obj].tail(VOLUME_STATS_LOOKBACK_DAYS)
        avg_volume_30d = float(prior["Volume"].mean()) if len(prior) else None

        volume_on_entry_day = None
        if trade_date_obj in daily.index:
            volume_on_entry_day = float(daily.loc[trade_date_obj, "Volume"])

        relative_volume = (
            round(volume_on_entry_day / avg_volume_30d, 3)
            if volume_on_entry_day is not None and avg_volume_30d
            else None
        )

        return {
            "volume_on_entry_day": int(volume_on_entry_day) if volume_on_entry_day is not None else None,
            "avg_volume_30d": round(avg_volume_30d, 1) if avg_volume_30d else None,
            "relative_volume": relative_volume,
            "float_shares": None,  # see this function's docstring -- filled in later via /fetch-float
            "avg_volume_tag": classify_avg_volume(avg_volume_30d),
            "rvol_tag": classify_relative_volume(relative_volume),
            "float_tag": classify_float(None),  # "float_unknown" until /fetch-float runs for this trade
        }
    except Exception as e:
        log.warning("Volume/float stats failed for %s on %s: %s", symbol, trade_date_obj, e)
        return empty


def _find_pivot_levels(daily: pd.DataFrame, window: int = 3, cluster_pct: float = 0.015, max_levels: int = 4) -> dict:
    """Cheap, no-LLM support/resistance: a bar is a pivot high/low if its
    High/Low is the extreme within +/-window bars either side, then nearby
    pivots (within cluster_pct of each other) are merged into one level
    weighted by how many times price touched that zone. This exists as a
    fast fallback the /generate-daily-chart endpoint always returns, and as
    a sanity check alongside whatever the LLM step comes back with -- it
    doesn't cost an API call."""
    if daily.empty or len(daily) < window * 2 + 1:
        return {"support": [], "resistance": []}

    highs = daily["High"].values
    lows = daily["Low"].values
    closes = daily["Close"].values
    last_close = float(closes[-1])
    n = len(daily)

    pivot_highs, pivot_lows = [], []
    for i in range(window, n - window):
        wl, wh = i - window, i + window + 1
        if highs[i] == highs[wl:wh].max():
            pivot_highs.append(float(highs[i]))
        if lows[i] == lows[wl:wh].min():
            pivot_lows.append(float(lows[i]))

    def cluster(prices):
        if not prices:
            return []
        prices = sorted(prices)
        clusters = [[prices[0]]]
        for p in prices[1:]:
            if abs(p - clusters[-1][-1]) / clusters[-1][-1] <= cluster_pct:
                clusters[-1].append(p)
            else:
                clusters.append([p])
        levels = [{"price": round(sum(c) / len(c), 2), "touches": len(c)} for c in clusters]
        levels.sort(key=lambda lv: lv["touches"], reverse=True)
        return levels[:max_levels]

    resistance = [lv for lv in cluster(pivot_highs) if lv["price"] >= last_close]
    support = [lv for lv in cluster(pivot_lows) if lv["price"] <= last_close]
    # A pivot cluster can land on the "wrong" side of the last close (e.g. an
    # old high the price has since blown through) -- that's fine to drop
    # since it's no longer a live level to watch going forward.
    resistance.sort(key=lambda lv: lv["price"])
    support.sort(key=lambda lv: lv["price"], reverse=True)
    return {"support": support, "resistance": resistance}


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """VWAP resets every session at SESSION_VWAP_START (4:00 AM ET pre-market
    open) so it includes pre-market volume — the standard anchor for small-cap
    gap-and-go setups. EMA/MACD run continuously across the whole fetched
    series so they're properly warmed up by the display window."""
    df = df.copy()

    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
    pv = typical_price * df["Volume"]
    session_date = df.index.date
    in_vwap_session = df.index.time >= SESSION_VWAP_START

    vol_for_vwap = df["Volume"].where(in_vwap_session, 0.0)
    pv_for_vwap = pv.where(in_vwap_session, 0.0)
    df["cum_vol"] = vol_for_vwap.groupby(session_date).cumsum()
    df["cum_pv"] = pv_for_vwap.groupby(session_date).cumsum()
    df["VWAP"] = df["cum_pv"] / df["cum_vol"]

    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()

    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"] = df["MACD"] - df["MACD_signal"]

    return df


def nearest_row(df: pd.DataFrame, ts: datetime) -> pd.Series:
    idx = df.index.get_indexer([ts], method="nearest")[0]
    return df.iloc[idx]


def compute_trade_levels(full_bars: pd.DataFrame, entry_dt: datetime, entry_price: float, side: str) -> dict:
    """Rule-based read of the setup + a chart-grounded stop/target, computed
    from real levels (swing points, VWAP/EMA9) so the LLM has defensible
    numbers instead of guessing. side is 'long' or 'short'."""
    is_long = side != "short"
    pre_entry = full_bars.loc[:entry_dt]
    lookback = pre_entry.tail(30)  # ~30 min of structure before entry

    avg_vol = pre_entry["Volume"].tail(20).mean() or 1.0
    entry_bar_vol = pre_entry["Volume"].iloc[-1] if len(pre_entry) else 0.0
    at_entry = nearest_row(full_bars, entry_dt)
    vwap_e, ema9_e = float(at_entry["VWAP"]), float(at_entry["EMA9"])

    recent_high = float(lookback["High"].max()) if len(lookback) else entry_price
    recent_low = float(lookback["Low"].min()) if len(lookback) else entry_price
    prior_high = float(lookback["High"].iloc[:-1].max()) if len(lookback) > 1 else recent_high

    breakout = is_long and entry_price >= prior_high * 0.999 and entry_bar_vol >= 1.5 * avg_vol
    dip_buy = is_long and abs(entry_price - vwap_e) / max(entry_price, 1) < 0.006 or (is_long and abs(entry_price - ema9_e) / max(entry_price, 1) < 0.006)
    setup_type = "breakout" if breakout else ("dip_buy" if dip_buy else "other")

    if is_long:
        swing_stop = recent_low - (recent_high - recent_low) * 0.05
        stop_price = min(swing_stop, vwap_e - 0.01) if setup_type == "dip_buy" else swing_stop
        stop_price = min(stop_price, entry_price - 0.01)
        risk = entry_price - stop_price
        target_price = entry_price + risk * 2
    else:
        swing_stop = recent_high + (recent_high - recent_low) * 0.05
        stop_price = max(swing_stop, vwap_e + 0.01) if setup_type == "dip_buy" else swing_stop
        stop_price = max(stop_price, entry_price + 0.01)
        risk = stop_price - entry_price
        target_price = entry_price - risk * 2

    reward = abs(target_price - entry_price)
    return {
        "setup_type": setup_type,
        "stop_price": round(stop_price, 4),
        "target_price": round(target_price, 4),
        "risk_per_share": round(abs(risk), 4),
        "reward_per_share": round(reward, 4),
        "r_multiple": 2,
    }


def serialize_bars(df: pd.DataFrame) -> list:
    """Display-window bars -> plain JSON-able dicts for the dashboard's
    client-side candlestick chart. Timestamps are naive local (ET) strings --
    the frontend renders them as wall-clock time, same as the PNG chart does,
    so it never has to reason about the ET tzinfo itself."""
    out = []
    for ts, row in df.iterrows():
        out.append({
            "t": ts.strftime("%Y-%m-%dT%H:%M:%S"),
            "o": round(float(row["Open"]), 4),
            "h": round(float(row["High"]), 4),
            "l": round(float(row["Low"]), 4),
            "c": round(float(row["Close"]), 4),
            "v": int(row["Volume"]),
            "vwap": round(float(row["VWAP"]), 4),
            "ema9": round(float(row["EMA9"]), 4),
            "ema20": round(float(row["EMA20"]), 4),
            "macd": round(float(row["MACD"]), 4),
            "macd_signal": round(float(row["MACD_signal"]), 4),
            "macd_hist": round(float(row["MACD_hist"]), 4),
        })
    return out


def render_chart(df: pd.DataFrame, symbol: str, entry_dt, exit_dt, entry_price, exit_price,
                  vwap_at_entry, ema9_at_entry, ema20_at_entry,
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
            vwap_at_entry, ema9_at_entry, ema20_at_entry,
            stop_price, target_price, better_entry, better_exit,
        )
    finally:
        RENDER_LOCK.release()


def _render_chart_locked(df, symbol, entry_dt, exit_dt, entry_price, exit_price,
                          macd_panel, overlays, style,
                          vwap_at_entry, ema9_at_entry, ema20_at_entry,
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
    # The line color itself is enough to tell VWAP/EMA9/EMA20 apart, so the
    # label just carries each one's price at entry instead of spelling out
    # the color or the VWAP session-reset note.
    legend_lines = [
        Line2D([0], [0], color="orange", lw=1.3, label=f"VWAP  ${vwap_at_entry:.2f}"),
        Line2D([0], [0], color="gray", lw=1.0, label=f"EMA 9  ${ema9_at_entry:.2f}"),
        Line2D([0], [0], color="blue", lw=1.0, label=f"EMA 20  ${ema20_at_entry:.2f}"),
    ]
    # Placed above the axes (not inside upper-left corner) so it can never
    # collide with the entry/exit labels, which also live near the top.
    price_ax.legend(
        handles=legend_lines, loc="lower center", bbox_to_anchor=(0.5, 1.01),
        ncol=3, fontsize=8, framealpha=0.9, borderaxespad=0,
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
            x = int(df.index.get_indexer([pd.Timestamp(dt_val)], method="nearest")[0])
        except Exception:
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
        p, t = body.get(price_key), body.get(time_key)
        if p is None or t is None:
            return None
        return {"price": float(p), "time": f"{trade_date}T{t}"}

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
        "macd_at_entry": round(float(at_entry["MACD"]), 4),
        "macd_signal_at_entry": round(float(at_entry["MACD_signal"]), 4),
        "macd_hist_at_entry": round(float(at_entry["MACD_hist"]), 4),
        "macd_hist_prior_bar": round(float(prior_macd_hist), 4) if prior_macd_hist is not None else None,
        "entry_vs_vwap": "above" if entry_price > at_entry["VWAP"] else "below",
        "entry_vs_ema9": "above" if entry_price > at_entry["EMA9"] else "below",
        "entry_vs_ema20": "above" if entry_price > at_entry["EMA20"] else "below",
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


# ---------------------------------------------------------------------------
# ORB / gap-gainer backtester -- powers the "Backtester" tab in the dashboard
# (backtester.html). Runs the same standalone engine.py / orb_strategy.py /
# polygon_client.py pipeline as the local orb-backtester-site tool, just as a
# background job polled from the browser (start -> poll status) instead of a
# CLI/Flask-template UI, since a multi-day scan can take a while under
# Polygon's free-tier rate limit and we don't want to block the request.
# ---------------------------------------------------------------------------
import json
import uuid
import re
from engine import BacktestConfig, run_backtest, compute_stats, BacktestCancelled
from orb_strategy import DEFAULT_PARAMS as ORB_DEFAULT_PARAMS
import backtest_storage
import strategy_store
import float_shares_store
from supabase_auth import resolve_user_id

# Backtest history/reports used to live in backtest_history.json /
# backtest_reports/<job_id>.json on local disk -- moved into Supabase
# (see backtest_storage.py) since Render's disk is ephemeral and this is
# real user-facing history, not throwaway job-progress state. Scoped
# per-account via resolve_user_id (same pattern /import-trades uses) --
# see backtest_storage.py's docstring for the full story on why this used
# to be shared/anonymous and isn't anymore.
_JOB_ID_RE = re.compile(r"^[0-9a-fA-F]+$")  # job ids are uuid4().hex[:12] -- reject anything else before touching Supabase


def _require_user():
    """Resolves the calling user from the Authorization header. Returns
    (user_id, None) on success, or (None, (response, status)) to return
    straight from the route on failure -- so every /backtest/* route below
    starts with:
        user_id, err = _require_user()
        if err: return err
    """
    user_id = resolve_user_id(request.headers.get("Authorization"))
    if not user_id:
        return None, (jsonify({"error": "missing or invalid Authorization token -- please log in and try again"}), 401)
    return user_id, None


@app.route("/fetch-float", methods=["POST", "OPTIONS"])
def fetch_float():
    """POST {trade_id, symbol} -> {float_shares, float_tag}. Requires
    Authorization: Bearer <supabase JWT> (see _require_user).

    On-demand replacement for the float lookup compute_volume_float_stats
    used to run automatically on every /generate-chart call -- see that
    function's docstring. Wired to a "Get float" button on the trade
    detail page (trade.js), so a live Polygon call for a symbol's float
    now only happens when someone actually asks for it, not for every
    trade that gets published.

    _fetch_float_shares below still only ever costs Polygon one real call
    per symbol, ever, across every trade and every account (see
    float_shares_store.py's in-memory + Supabase caching) -- this route
    doesn't change that. It just moves WHEN that call can get triggered:
    from "automatically, on every trade, whether anyone looks or not" to
    "on request, the first time someone actually wants that symbol's
    float".

    Also merges the result into this one trade's stored
    trade_details.indicators and trades.float_tag in Supabase (see
    float_shares_store.save_float_to_trade), scoped to the calling user,
    so re-opening the same trade later shows the float without another
    round trip here."""
    if request.method == "OPTIONS":
        return "", 204

    user_id, err = _require_user()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    trade_id = (body.get("trade_id") or "").strip()
    symbol = (body.get("symbol") or "").strip().upper()
    if not trade_id or not symbol:
        return jsonify({"error": "trade_id and symbol are required"}), 400

    try:
        shares = _fetch_float_shares(symbol)
    except Exception as e:
        log.warning("On-demand float lookup failed for %s: %s", symbol, e)
        return jsonify({"error": f"Float lookup failed: {e}"}), 502

    float_tag = classify_float(shares)

    try:
        float_shares_store.save_float_to_trade(user_id, trade_id, shares, float_tag)
    except Exception as e:
        # The Polygon lookup already succeeded -- don't fail the request
        # (and cost the user another click/another Polygon round trip
        # next time) just because persisting it hit a snag. Worst case
        # this trade just re-saves next time its "Get float" is clicked.
        log.warning("Saving float for trade %s (%s) failed (non-fatal): %s", trade_id, symbol, e)

    return jsonify({"float_shares": shares, "float_tag": float_tag})


# ---------------------------------------------------------------------------
# Bulk "fill missing floats" job -- same start/poll/cancel shape as the
# backtester job below (_backtest_jobs), just walking every symbol this
# user has a trade on with no float yet instead of every trading day in a
# date range. Wired to a button on the journal page (journal.html) rather
# than a per-trade one, so someone doesn't have to open every trade that
# predates this feature (or got imported in bulk) and click "Get float"
# one at a time.
#
# Runs one live Polygon call per *symbol* (not per trade) -- multiple
# trades on the same symbol share one _fetch_float_shares call, same
# caching/rate-limiting as the single-trade /fetch-float route above --
# then writes the result onto every one of that symbol's trades via
# save_float_to_trade, so each one shows its float next time it's opened
# without needing its own "Get float" click.
# ---------------------------------------------------------------------------
_float_bulk_jobs = {}
_float_bulk_jobs_lock = threading.Lock()


def _run_float_bulk_job(job_id: str, user_id: str, symbol_to_trade_ids: dict):
    def cancelled():
        with _float_bulk_jobs_lock:
            job = _float_bulk_jobs.get(job_id)
            return bool(job and job.get("cancel_requested"))

    symbols = sorted(symbol_to_trade_ids.keys())
    for i, symbol in enumerate(symbols):
        if cancelled():
            with _float_bulk_jobs_lock:
                _float_bulk_jobs[job_id]["status"] = "cancelled"
            return

        with _float_bulk_jobs_lock:
            _float_bulk_jobs[job_id]["current_symbol"] = symbol
            _float_bulk_jobs[job_id]["current_index"] = i  # symbols fully done so far

        trade_ids = symbol_to_trade_ids[symbol]
        try:
            shares = _fetch_float_shares(symbol)
            float_tag = classify_float(shares)
            for trade_id in trade_ids:
                try:
                    float_shares_store.save_float_to_trade(user_id, trade_id, shares, float_tag)
                except Exception as e:
                    log.warning("Bulk float job %s: saving %s (trade %s) failed: %s", job_id, symbol, trade_id, e)
            with _float_bulk_jobs_lock:
                job = _float_bulk_jobs[job_id]
                job["trades_updated"] += len(trade_ids)
                job["updated"].append({"symbol": symbol, "float_shares": shares, "float_tag": float_tag, "trade_ids": trade_ids})
        except Exception as e:
            log.warning("Bulk float job %s: lookup for %s failed: %s", job_id, symbol, e)
            with _float_bulk_jobs_lock:
                _float_bulk_jobs[job_id]["errors"].append({"symbol": symbol, "error": str(e)})

        with _float_bulk_jobs_lock:
            _float_bulk_jobs[job_id]["current_index"] = i + 1  # this symbol is now done too

    with _float_bulk_jobs_lock:
        if _float_bulk_jobs[job_id]["status"] == "running":
            _float_bulk_jobs[job_id]["status"] = "done"


@app.route("/fetch-float/bulk/start", methods=["POST", "OPTIONS"])
def fetch_float_bulk_start():
    """Kicks off a background job that finds every trade of the calling
    user's with no float yet (see float_shares_store.list_trades_missing_float),
    groups them by symbol, and works through the symbol list one at a time
    (see _run_float_bulk_job), same start->poll->cancel shape as
    /backtest/start below. Returns immediately with a job_id -- poll
    /fetch-float/bulk/status/<job_id> for progress."""
    if request.method == "OPTIONS":
        return "", 204

    user_id, err = _require_user()
    if err:
        return err

    try:
        missing = float_shares_store.list_trades_missing_float(user_id)
    except Exception as e:
        log.warning("list_trades_missing_float failed for user %s: %s", user_id, e)
        return jsonify({"error": f"Couldn't look up trades: {e}"}), 502

    symbol_to_trade_ids: dict = {}
    for row in missing:
        symbol = (row.get("symbol") or "").strip().upper()
        trade_id = row.get("id")
        if not symbol or not trade_id:
            continue
        symbol_to_trade_ids.setdefault(symbol, []).append(trade_id)

    job_id = uuid.uuid4().hex[:12]
    with _float_bulk_jobs_lock:
        _float_bulk_jobs[job_id] = {
            "status": "running" if symbol_to_trade_ids else "done",
            "total_symbols": len(symbol_to_trade_ids),
            "current_index": 0,
            "current_symbol": None,
            "total_trades": sum(len(v) for v in symbol_to_trade_ids.values()),
            "trades_updated": 0,
            "updated": [],
            "errors": [],
            "cancel_requested": False,
            "user_id": user_id,  # stripped before responding, same as _backtest_jobs
        }

    if symbol_to_trade_ids:
        t = threading.Thread(target=_run_float_bulk_job, args=(job_id, user_id, symbol_to_trade_ids), daemon=True)
        t.start()
    return jsonify({"job_id": job_id, "total_symbols": len(symbol_to_trade_ids), "total_trades": sum(len(v) for v in symbol_to_trade_ids.values())})


@app.route("/fetch-float/bulk/status/<job_id>", methods=["GET"])
def fetch_float_bulk_status(job_id):
    user_id, err = _require_user()
    if err:
        return err
    with _float_bulk_jobs_lock:
        job = _float_bulk_jobs.get(job_id)
    if job is None or job.get("user_id") != user_id:
        return jsonify({"status": "unknown"}), 404
    return jsonify({k: v for k, v in job.items() if k != "user_id"})


@app.route("/fetch-float/bulk/cancel/<job_id>", methods=["POST", "OPTIONS"])
def fetch_float_bulk_cancel(job_id):
    # Cooperative cancel, same as /backtest/cancel: flips a flag
    # _run_float_bulk_job checks between symbols. The symbol currently
    # in flight finishes (it's one Polygon call plus a handful of small
    # writes, never long), everything after it stops. Whatever ran
    # already stays saved on those trades.
    if request.method == "OPTIONS":
        return "", 204
    user_id, err = _require_user()
    if err:
        return err
    with _float_bulk_jobs_lock:
        job = _float_bulk_jobs.get(job_id)
        if job is None or job.get("user_id") != user_id:
            return jsonify({"error": "unknown job"}), 404
        if job["status"] != "running":
            return jsonify({"status": job["status"]})
        job["cancel_requested"] = True
    return jsonify({"status": "cancelling"})


_backtest_jobs = {}
_backtest_jobs_lock = threading.Lock()


@app.after_request
def _add_cors_headers(resp):
    # This service is called directly from the browser (backtester.js) as
    # well as from n8n -- CORS only matters for the browser calls, and is a
    # no-op for server-to-server ones, so it's safe to apply to every route.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, ngrok-skip-browser-warning"
    return resp


def _backtest_report_load(job_id, user_id):
    # job ids are always uuid4().hex[:12] -- reject anything else before
    # it goes anywhere near a query, same guard the old file path had.
    if not _JOB_ID_RE.match(job_id or ""):
        return None
    return backtest_storage.load_backtest_report(job_id, user_id)


def _backtest_report_delete(job_id, user_id):
    if not _JOB_ID_RE.match(job_id or ""):
        return
    backtest_storage.delete_backtest_run(job_id, user_id)


def _run_backtest_job(job_id: str, user_id: str, cfg: BacktestConfig, meta: dict):
    def progress(i, total, d, trades_so_far=None):
        with _backtest_jobs_lock:
            job = _backtest_jobs.get(job_id)
            if job is None:
                return
            job["current"] = i + 1
            job["total"] = total
            job["day"] = d.isoformat()
            # Keep partial trades + stats up to date after every day so a
            # mid-run poll (or a cancel) still has something real to show,
            # instead of only ever populating these once the whole run ends.
            if trades_so_far is not None:
                job["trades"] = trades_so_far
                job["stats"] = compute_stats(trades_so_far, starting_capital=cfg.starting_capital)

    def cancel_check():
        # Polled before every symbol (see engine.py), not just once per
        # day, so hitting Cancel takes effect within a couple seconds
        # instead of waiting for the whole in-flight day to finish.
        with _backtest_jobs_lock:
            job = _backtest_jobs.get(job_id)
            return bool(job and job.get("cancel_requested"))

    try:
        trades = run_backtest(cfg, progress_cb=progress, cancel_check=cancel_check)
        stats = compute_stats(trades, starting_capital=cfg.starting_capital)

        with _backtest_jobs_lock:
            _backtest_jobs[job_id]["status"] = "done"
            _backtest_jobs[job_id]["stats"] = stats
            _backtest_jobs[job_id]["trades"] = trades

        created_at = datetime.now().isoformat(timespec="seconds")
        label = meta.get("label") or "(untitled run)"
        summary_stats = {
            "num_trades": stats["num_trades"],
            "win_rate": stats["win_rate"],
            "profit_factor": stats["profit_factor"],
            "net_pnl_dollars": stats["net_pnl_dollars"],
            "avg_r": stats["avg_r"],
            "max_drawdown_dollars": stats["max_drawdown_dollars"],
            "total_commissions_dollars": stats.get("total_commissions_dollars", 0.0),
        }
        # One upsert covers both the Past Runs list (summary_stats) and the
        # full report (every trade, full stats incl. equity curve) -- see
        # GET /backtest/history/<job_id>/report for the latter.
        try:
            backtest_storage.save_backtest_run(
                job_id, user_id, created_at, label, meta, summary_stats,
                report={
                    "id": job_id, "created_at": created_at, "label": label,
                    "params": meta, "stats": stats, "trades": trades,
                },
            )
        except Exception:
            # Don't let a Supabase hiccup lose the run entirely -- it's
            # still sitting in _backtest_jobs and pollable/downloadable
            # until this process restarts, just won't show up in Past Runs.
            log.exception("Backtest job %s: failed to persist to Supabase", job_id)
    except BacktestCancelled:
        log.info("Backtest job %s cancelled by user", job_id)
        with _backtest_jobs_lock:
            job = _backtest_jobs[job_id]
            job["status"] = "cancelled"
            # job["trades"]/job["stats"] already hold whatever was finished
            # as of the last completed day -- leave them as-is so the
            # person can still see/download the partial report.
    except Exception as e:
        log.exception("Backtest job %s failed", job_id)
        with _backtest_jobs_lock:
            _backtest_jobs[job_id]["status"] = "error"
            _backtest_jobs[job_id]["error"] = str(e)


def _num(body, name, default, cast=float):
    v = body.get(name, default)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


@app.route("/backtest/defaults", methods=["GET"])
def backtest_defaults():
    """So backtester.js doesn't have to hardcode a second copy of
    orb_strategy.py's defaults -- it fetches this once to prefill the form."""
    return jsonify(dict(ORB_DEFAULT_PARAMS))


@app.route("/backtest/start", methods=["POST", "OPTIONS"])
def backtest_start():
    if request.method == "OPTIONS":
        return "", 204

    user_id, err = _require_user()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}

    try:
        start = datetime.strptime(body.get("start", ""), "%Y-%m-%d").date()
        end = datetime.strptime(body.get("end", ""), "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "start/end must be YYYY-MM-DD"}), 400
    if end < start:
        return jsonify({"error": "end date is before start date"}), 400
    if end > date.today():
        return jsonify({"error": "end date can't be in the future"}), 400

    defaults = ORB_DEFAULT_PARAMS
    strategy_params = {
        "entry_mode": body.get("entry_mode", defaults["entry_mode"]),
        "orb_minutes": _num(body, "orb_minutes", defaults["orb_minutes"], int),
        "entry_after_orb": bool(body.get("entry_after_orb", defaults["entry_after_orb"])),
        "donchian_lookback": _num(body, "donchian_lookback", defaults["donchian_lookback"], int),
        "stop_mode": body.get("stop_mode", defaults["stop_mode"]),
        "fixed_stop_cents": _num(body, "fixed_stop_cents", defaults["fixed_stop_cents"]),
        "fixed_stop_pct": _num(body, "fixed_stop_pct", defaults["fixed_stop_pct"]),
        "atr_period": _num(body, "atr_period", defaults["atr_period"], int),
        "atr_mult": _num(body, "atr_mult", defaults["atr_mult"]),
        "breakeven_after_cents": _num(body, "breakeven_after_cents", None) or None,
        "ema_period": _num(body, "ema_period", defaults.get("ema_period", 9), int),
        "macd_fast": _num(body, "macd_fast", defaults.get("macd_fast", 12), int),
        "macd_slow": _num(body, "macd_slow", defaults.get("macd_slow", 26), int),
        "macd_signal": _num(body, "macd_signal", defaults.get("macd_signal", 9), int),
        "rsi_period": _num(body, "rsi_period", defaults.get("rsi_period", 14), int),
        "rsi_oversold": _num(body, "rsi_oversold", defaults.get("rsi_oversold", 30.0)),
        "target_r": _num(body, "target_r", defaults["target_r"]) or None,
        "time_stop_minutes": _num(body, "time_stop_minutes", None) or None,
        "time_stop_min_gain_cents": _num(body, "time_stop_min_gain_cents", 0.0),
        "giveback_cents": _num(body, "giveback_cents", None) or None,
        "giveback_pct": _num(body, "giveback_pct", None) or None,
        "giveback_arm_cents": _num(body, "giveback_arm_cents", 0.0),
        "stall_exit": bool(body.get("stall_exit", False)),
        "ema_close_exit": bool(body.get("ema_close_exit", defaults.get("ema_close_exit", False))),
        "require_macd_confirmation": bool(body.get("require_macd_confirmation", defaults.get("require_macd_confirmation", False))),
        # Re-entry: on by default (see orb_strategy.py's DEFAULT_PARAMS) --
        # a symbol/day can produce more than one trade unless the request
        # explicitly turns it off.
        "allow_reentry": bool(body.get("allow_reentry", defaults.get("allow_reentry", True))),
        "max_trades_per_day": _num(body, "max_trades_per_day", defaults.get("max_trades_per_day", 3), int),
        "reentry_cooldown_minutes": _num(body, "reentry_cooldown_minutes", defaults.get("reentry_cooldown_minutes", 0.0)),
        "slippage_bps": _num(body, "slippage_bps", defaults.get("slippage_bps", 5.0)),
        # Position building -- "buy some, add on strength" (see
        # orb_strategy.py's POSITION BUILDING section). Off unless the
        # request explicitly turns it on.
        "scale_in_enabled": bool(body.get("scale_in_enabled", False)),
        "scale_in_initial_size_pct": _num(body, "scale_in_initial_size_pct", defaults.get("scale_in_initial_size_pct", 50.0)),
        "scale_in_add_size_pct": _num(body, "scale_in_add_size_pct", defaults.get("scale_in_add_size_pct", 25.0)),
        "scale_in_max_adds": _num(body, "scale_in_max_adds", defaults.get("scale_in_max_adds", 2), int),
        "scale_in_hold_bars": _num(body, "scale_in_hold_bars", defaults.get("scale_in_hold_bars", 2), int),
        "scale_in_min_gain_cents": _num(body, "scale_in_min_gain_cents", defaults.get("scale_in_min_gain_cents", 10.0)),
        # Tightening trail-protect stop -- ratchets up to lock in a growing
        # fraction of the peak gain as the trade's peak R climbs. Off
        # unless the request explicitly turns it on. Ladder is sent (and
        # returned by /backtest/defaults) as a flat list of [r, protect_frac]
        # pairs -- JSON has no tuples, but a 2-element array round-trips
        # through jsonify() the same way orb_strategy.py's tuple defaults do.
        "trail_protect_enabled": bool(body.get("trail_protect_enabled", False)),
        "trail_protect_ladder": [
            (float(rung[0]), float(rung[1]))
            for rung in (body.get("trail_protect_ladder") or [])
            if isinstance(rung, (list, tuple)) and len(rung) == 2
        ] or list(defaults.get("trail_protect_ladder", [])),
        "flatten_time": body.get("flatten_time", defaults["flatten_time"]),
        # Was hardcoded to "09:30" (regular-hours open) regardless of what
        # the form/AI-config panel sent, which silently threw away any
        # "start scanning at <time>" instruction (e.g. a pre-market ORB
        # session) -- fetch_minute_bars already returns pre/post-market
        # bars, orb_strategy already accepts session_open, this just wires
        # the request body's value through instead of ignoring it.
        "session_open": body.get("session_start", defaults.get("session_open", "09:30")),
    }

    cfg = BacktestConfig(
        start_date=start, end_date=end,
        top_n=int(_num(body, "top_n", 5, int)),
        min_price=_num(body, "min_price", 1.0), max_price=_num(body, "max_price", 50.0),
        min_dollar_volume=_num(body, "min_dollar_volume", 5_000_000),
        min_gap_pct=_num(body, "min_gap_pct", 5.0),
        position_size_dollars=_num(body, "position_size", 2000.0),
        strategy_params=strategy_params,
        include_commissions=bool(body.get("include_commissions", True)),
        starting_capital=_num(body, "starting_capital", 25_000.0),
        position_sizing_mode=body.get("position_sizing_mode", "fixed_dollars"),
        position_size_pct=_num(body, "position_size_pct", 10.0),
        risk_pct_of_capital=_num(body, "risk_pct_of_capital", 1.0),
    )

    job_id = uuid.uuid4().hex[:12]
    with _backtest_jobs_lock:
        _backtest_jobs[job_id] = {
            "status": "running", "current": 0, "total": 0, "day": None,
            "trades": [], "stats": None, "cancel_requested": False,
            "user_id": user_id,  # not serialized out -- see backtest_status, which strips it before responding
        }

    t = threading.Thread(target=_run_backtest_job, args=(job_id, user_id, cfg, body), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/backtest/status/<job_id>", methods=["GET"])
def backtest_status(job_id):
    user_id, err = _require_user()
    if err:
        return err
    with _backtest_jobs_lock:
        job = _backtest_jobs.get(job_id)
    # Someone else's job_id (or an unknown one) looks identical to a
    # missing job -- don't distinguish "exists but isn't yours" from
    # "never existed".
    if job is None or job.get("user_id") != user_id:
        return jsonify({"status": "unknown"}), 404
    return jsonify({k: v for k, v in job.items() if k != "user_id"})


@app.route("/backtest/cancel/<job_id>", methods=["POST", "OPTIONS"])
def backtest_cancel(job_id):
    # Cooperative cancel: flips a flag that _run_backtest_job's progress()
    # callback checks once it finishes the day currently in flight (that
    # in-flight day can't be interrupted mid-fetch, but everything after
    # it stops). Whatever trades/stats had already accumulated stay on the
    # job as a partial report -- see backtest_status.
    if request.method == "OPTIONS":
        return "", 204
    user_id, err = _require_user()
    if err:
        return err
    with _backtest_jobs_lock:
        job = _backtest_jobs.get(job_id)
        if job is None or job.get("user_id") != user_id:
            return jsonify({"error": "unknown job"}), 404
        if job["status"] != "running":
            return jsonify({"status": job["status"]})
        job["cancel_requested"] = True
    return jsonify({"status": "cancelling"})


@app.route("/backtest/history", methods=["GET"])
def backtest_history():
    user_id, err = _require_user()
    if err:
        return err
    return jsonify(backtest_storage.load_backtest_history(user_id))


@app.route("/backtest/history/<job_id>/report", methods=["GET"])
def backtest_history_report(job_id):
    # Full report for a past run -- every trade + full stats (equity curve
    # included) -- saved once at run-completion time (see
    # _run_backtest_job) and readable here at any point after, independent
    # of the in-memory _backtest_jobs dict, which is lost on a server
    # restart. This is what lets a Past Runs card reopen the actual report
    # instead of only re-running the same params. Scoped to the caller's
    # own runs -- see _backtest_report_load / backtest_storage.load_backtest_report.
    user_id, err = _require_user()
    if err:
        return err
    report = _backtest_report_load(job_id, user_id)
    if report is None:
        return jsonify({"error": "no saved report for this run (may predate this feature, may not be yours, or the run didn't finish)"}), 404
    # Once a run's charts have been generated, each trade's `bars` is a
    # full session's worth of 1-min OHLCV -- easily the overwhelming
    # majority of this payload's weight for a run of any real size, and
    # the report view only ever needs it lazily, one trade at a time, when
    # something is actually clicked (View Chart, a Best/Worst stat, an
    # Overview breakdown row). Strip it out of the default response --
    # replaced with a cheap has_bars flag so the UI still knows whether a
    # chart is available -- and serve it through the dedicated per-trade
    # endpoint below instead. ?full=1 restores the old all-at-once shape,
    # for anything that genuinely still needs every trade's bars at once.
    if request.args.get("full") != "1":
        trades = report.get("trades") or []
        report = dict(report)
        stripped = []
        for t in trades:
            t2 = {k: v for k, v in t.items() if k != "bars"}
            t2["has_bars"] = bool(t.get("bars"))
            stripped.append(t2)
        report["trades"] = stripped
    return jsonify(report)


@app.route("/backtest/history/<job_id>/report/trade-bars/<int:idx>", methods=["GET"])
def backtest_history_trade_bars(job_id, idx):
    """Lazily fetches one trade's bars by its index into the saved
    report's `trades` array (stable regardless of how the report page has
    the table currently sorted -- it's an index into the stored order,
    not whatever order is on screen). Backs the report page's on-demand
    chart views now that GET /report strips bars out by default (see
    above)."""
    user_id, err = _require_user()
    if err:
        return err
    report = _backtest_report_load(job_id, user_id)
    if report is None:
        return jsonify({"error": "no saved report for this run (may predate this feature, may not be yours, or the run didn't finish)"}), 404
    trades = report.get("trades") or []
    if idx < 0 or idx >= len(trades):
        return jsonify({"error": "trade index out of range"}), 404
    return jsonify({"bars": trades[idx].get("bars") or []})


@app.route("/backtest/history/<job_id>/enrich", methods=["POST", "OPTIONS"])
def backtest_history_enrich(job_id):
    """Callback target for the n8n trade-journal workflow's per-trade
    chart+vision-LLM pass on a backtest's trades (see sendToJournal() in
    backtester.js). Deliberately separate from data/trades.json and the
    shared Google Sheet real fills write to -- this only ever touches this
    ONE run's own backtest_reports/<job_id>.json, so a backtest never
    shows up in, or skews the stats of, the real trading dashboard. Each
    run stays its own self-contained mini report.

    Body: {"trades": [{"date", "symbol", "entry_time", <any of: verdict,
    bars, indicators, chart_image (a data: URL or http(s) URL, kept only
    as a fallback for consumers that can't render an interactive chart),
    lessons, better_entry_price, better_entry_reason, better_exit_price,
    better_exit_reason>, ...}, ...]}. `bars`/`indicators` are the same
    per-minute series /generate-chart already computes (see
    serialize_bars/compute_indicators above) -- passing them through lets
    the report page draw its own interactive candlestick chart with
    entry/exit markers client-side, the same way trade.js does for real
    journal trades, instead of only having a flat PNG to show. Each
    incoming trade is matched to one already in the saved report by
    (date, symbol, entry_time) and only the enrichment fields present are
    merged onto it -- fields already on the trade (P&L, commission, etc.)
    are left untouched. Unmatched incoming trades don't fail the whole
    batch, just get reported back under `unmatched` so a partial/retried
    n8n run doesn't lose whatever *did* match.
    """
    if request.method == "OPTIONS":
        return "", 204

    # Deliberately unauthenticated (unlike the other /backtest/history/*
    # routes): the caller here is n8n's server-to-server workflow, not the
    # logged-in person's browser, so there's no Supabase access token to
    # check -- see backtest_storage.save_backtest_report's docstring.
    if not _JOB_ID_RE.match(job_id or ""):
        return jsonify({"error": "no saved report for this run"}), 404
    report = backtest_storage.load_backtest_report_unscoped(job_id)
    if report is None:
        return jsonify({"error": "no saved report for this run"}), 404

    body = request.get_json(force=True, silent=True) or {}
    incoming = body.get("trades") or []
    ENRICH_FIELDS = (
        "verdict", "bars", "indicators", "chart_image", "lessons",
        "better_entry_price", "better_entry_reason",
        "better_exit_price", "better_exit_reason",
    )

    def key_of(t):
        return (t.get("date"), t.get("symbol"), t.get("entry_time"))

    by_key = {key_of(t): t for t in incoming}
    matched_keys = set()
    matched_but_empty = []
    for t in report.get("trades", []):
        src = by_key.get(key_of(t))
        if not src:
            continue
        for field in ENRICH_FIELDS:
            if field in src:
                t[field] = src[field]
        if "lessons" in src:
            # This callback's lessons come straight from n8n's own
            # Gemini call (backtester.js's "Send to Journal"), not
            # through daily_sync.py's normal per-fill pipeline, so they
            # never pass through _normalize_lessons there. Without this,
            # a double-encoded lesson (a JSON string instead of a real
            # object -- see _normalize_lessons's docstring) lands in
            # this run's saved report as-is and every consumer
            # (trade.js/quiz.js/rewind.js/share-export.js) prints it as
            # raw JSON text instead of the fields inside it.
            from daily_sync import _normalize_lessons
            t["lessons"] = _normalize_lessons(src["lessons"])
        matched_keys.add(key_of(t))
        if not (isinstance(t.get("bars"), list) and t["bars"]):
            matched_but_empty.append(key_of(t))

    unmatched_incoming = [k for k in by_key if k not in matched_keys]
    log.info(
        "Enrich %s: %d/%d incoming trades matched, %d matched-but-no-bars",
        job_id, len(matched_keys), len(incoming), len(matched_but_empty),
    )
    if unmatched_incoming:
        log.warning("Enrich %s: unmatched incoming keys (date, symbol, entry_time): %s", job_id, unmatched_incoming)
        log.warning("Enrich %s: keys already on the saved report: %s", job_id, [key_of(t) for t in report.get("trades", [])])
    if matched_but_empty:
        log.warning("Enrich %s: matched but bars came through empty/missing for: %s", job_id, matched_but_empty)

    backtest_storage.save_backtest_report(job_id, report)
    return jsonify({"matched": len(matched_keys), "unmatched": len(by_key) - len(matched_keys), "matched_but_empty": len(matched_but_empty)})


@app.route("/backtest/history/<job_id>", methods=["DELETE", "OPTIONS"])
def backtest_history_delete(job_id):
    if request.method == "OPTIONS":
        return "", 204
    user_id, err = _require_user()
    if err:
        return err
    # One row covers both the history-list entry and the full report now,
    # so deleting it removes both in a single call (used to be a
    # load-all/filter/save-all on the history file plus a separate report
    # file delete). Scoped to the caller's own runs -- someone else's
    # job_id matches zero rows and is a silent no-op, not an error.
    _backtest_report_delete(job_id, user_id)
    with _backtest_jobs_lock:
        job = _backtest_jobs.get(job_id)
        if job and job.get("user_id") == user_id:
            _backtest_jobs.pop(job_id, None)
    return jsonify({"deleted": job_id})


# --- Strategies: the shared bundle backtest and live-trading both read/write ---
# See strategy_store.py's docstring for the full shape. live-service reads
# these directly (imports strategy_store.py from its read-only mount of
# this repo) rather than over HTTP -- these routes are for web-service
# (backtester.js's "Save as strategy" button and, eventually, a
# strategy-management view) to list/create/delete them.

# A run's stored `params` (backtest_start's raw request body) mixes
# together three different kinds of settings: the gap-scan rule that
# picked candidate symbols (top_n/min_price/max_price/min_dollar_volume/
# min_gap_pct), the entry/stop/exit rule itself (entry_mode, stop_mode,
# target_r, ...), and backtest-only account simulation settings that have
# no live-trading equivalent (starting_capital, position_size, ...).
# Splitting those apart is exactly what turns "a completed backtest run"
# into "a strategy live-service can start a run from".
_SYMBOL_RULE_KEYS = {"top_n", "min_price", "max_price", "min_dollar_volume", "min_gap_pct"}
_BACKTEST_ONLY_KEYS = {
    "start", "end", "label", "position_size", "position_sizing_mode",
    "position_size_pct", "risk_pct_of_capital", "starting_capital",
    "include_commissions",
}


@app.route("/strategies", methods=["GET", "POST", "OPTIONS"])
def strategies_list_create():
    if request.method == "OPTIONS":
        return "", 204
    user_id, err = _require_user()
    if err:
        return err

    if request.method == "GET":
        return jsonify(strategy_store.list_strategies(user_id))

    # POST: create a strategy directly (not from a backtest run) -- e.g.
    # a future "new strategy" flow on the live-trading page itself.
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    entry_mode = body.get("entry_mode") or ""
    if not name or not entry_mode:
        return jsonify({"error": "name and entry_mode are required"}), 400
    strategy_id = uuid.uuid4().hex[:12]
    row = strategy_store.save_strategy(
        strategy_id, user_id, datetime.now().isoformat(timespec="seconds"),
        name, entry_mode,
        params=body.get("params") or {},
        symbol_rule=body.get("symbol_rule") or {"mode": "manual", "symbols": []},
        source_backtest_run_id=None,
        source_summary_stats={},
    )
    return jsonify(row)


@app.route("/strategies/<strategy_id>", methods=["GET", "DELETE", "OPTIONS"])
def strategies_get_delete(strategy_id):
    if request.method == "OPTIONS":
        return "", 204
    user_id, err = _require_user()
    if err:
        return err
    if request.method == "DELETE":
        strategy_store.delete_strategy(strategy_id, user_id)
        return jsonify({"deleted": strategy_id})
    row = strategy_store.get_strategy(strategy_id, user_id)
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(row)


@app.route("/backtest/history/<job_id>/save-strategy", methods=["POST", "OPTIONS"])
def backtest_save_strategy(job_id):
    """Turns a completed backtest run into a reusable strategy -- pulls
    entry_mode/params/symbol_rule out of the run's stored params (see the
    key-splitting comment above), and copies its summary_stats over as a
    point-in-time reference so a strategy picker can show how it
    backtested without a join back to backtest_runs (which may later get
    deleted)."""
    if request.method == "OPTIONS":
        return "", 204
    user_id, err = _require_user()
    if err:
        return err
    if not _JOB_ID_RE.match(job_id or ""):
        return jsonify({"error": "unknown run"}), 404

    run = backtest_storage.load_backtest_run_light(job_id, user_id)
    if run is None:
        return jsonify({"error": "unknown run"}), 404

    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    run_params = dict(run.get("params") or {})
    entry_mode = run_params.pop("entry_mode", None)
    if not entry_mode:
        return jsonify({"error": "this run has no entry_mode -- can't save as a strategy"}), 400

    symbol_rule = {
        "mode": "top_gappers",
        "top_n": run_params.pop("top_n", 5),
        "min_price": run_params.pop("min_price", 1.0),
        "max_price": run_params.pop("max_price", 50.0),
        "min_dollar_volume": run_params.pop("min_dollar_volume", 5_000_000),
        "min_gap_pct": run_params.pop("min_gap_pct", 5.0),
    }
    # Allow the request to override the rule (e.g. the person picked
    # specific symbols on the save dialog instead of keeping the scan
    # rule) -- otherwise keep what the run actually used.
    symbol_rule.update(body.get("symbol_rule") or {})

    strategy_params = {k: v for k, v in run_params.items() if k not in _BACKTEST_ONLY_KEYS}

    strategy_id = uuid.uuid4().hex[:12]
    row = strategy_store.save_strategy(
        strategy_id, user_id, datetime.now().isoformat(timespec="seconds"),
        name, entry_mode, strategy_params, symbol_rule,
        source_backtest_run_id=job_id,
        source_summary_stats=run.get("summary_stats") or {},
    )
    return jsonify(row)



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
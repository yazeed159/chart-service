"""
market_data.py
Bar-fetching (with process-lifetime caching), Polygon rate limiting, and
indicator/volume/float computation shared by charts.py and float_routes.py.
Split out of the old chart_service.py -- see chart_service.py's module
docstring for how these files fit together.
"""

import os
import time
import threading
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, date

import requests
import pandas as pd

import float_shares_store
import alpaca_client
from config import (
    log, POLYGON_API_KEY, WINDOW_BEFORE, WINDOW_AFTER, LOOKBACK_DAYS,
    ENABLE_VOLUME_FLOAT_STATS, VOLUME_STATS_LOOKBACK_DAYS, SR_LOOKBACK_DAYS_DEFAULT,
    POLYGON_BATCH_SIZE, POLYGON_BATCH_WINDOW_S, POLYGON_BATCH_MIN_GAP_S,
    ET, SESSION_VWAP_START, REGULAR_SESSION_START, REGULAR_SESSION_END,
    TICK_DATA_MAX_TRADES,
)

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

def _fetch_raw_bars_from_alpaca(symbol: str, trade_date_obj: date) -> pd.DataFrame:
    """Same shape as _fetch_raw_bars_from_polygon (ET-indexed Open/High/Low/
    Close/Volume/vwap_bar, gap-filled), sourced from Alpaca's historical
    minute bars. The free plan's full-market SIP feed is allowed for data
    >= 15 minutes old, so the window's end is clamped to now - 16 min."""
    start_et = datetime.combine(trade_date_obj - timedelta(days=_minute_bar_lookback_days()), datetime.min.time(), tzinfo=ET)
    end_et = datetime.combine(trade_date_obj, datetime.max.time().replace(microsecond=0), tzinfo=ET)
    cutoff = datetime.now(ET) - timedelta(minutes=16)
    if end_et > cutoff:
        end_et = cutoff
    if end_et <= start_et:
        raise ValueError(f"Alpaca: {trade_date_obj} is too recent to query yet (free plan needs data >= 15 min old)")
    raw = alpaca_client.get_minute_bars(symbol, start_et.astimezone(ZoneInfo("UTC")), end_et.astimezone(ZoneInfo("UTC")))
    if not raw:
        raise ValueError(f"Alpaca returned no minute bars for {symbol} between {start_et:%Y-%m-%d} and {end_et:%Y-%m-%d}")
    df = pd.DataFrame(raw)
    df["t"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET)
    df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume", "vw": "vwap_bar"})
    if "vwap_bar" not in df.columns:
        df["vwap_bar"] = df["Close"]
    df = df.set_index("t")[["Open", "High", "Low", "Close", "Volume", "vwap_bar"]].sort_index()
    return _fill_intraday_gaps(df)


def _has_trade_day(df: pd.DataFrame, trade_date_obj: date) -> bool:
    return df is not None and not df.empty and bool((df.index.date == trade_date_obj).any())


def _fetch_raw_bars(symbol: str, trade_date_obj: date) -> pd.DataFrame:
    """Minute bars from the configured provider, falling back to the other
    one when the first errors or has nothing on the trade date.
    BARS_PROVIDER env: "auto" (default -- Alpaca first when its keys are set,
    else Polygon), "alpaca", or "polygon". The Polygon free plan often lags a
    day or more on minute data; Alpaca's free plan serves full-market history
    older than 15 minutes."""
    mode = (os.environ.get("BARS_PROVIDER") or "auto").lower()
    if mode == "polygon":
        order = ["polygon"]
    elif mode == "alpaca":
        order = ["alpaca"]
    else:
        order = ["alpaca", "polygon"] if alpaca_client.available() else ["polygon"]

    errors = []
    last_df = None
    for provider in order:
        try:
            df = _fetch_raw_bars_from_alpaca(symbol, trade_date_obj) if provider == "alpaca" else _fetch_raw_bars_from_polygon(symbol, trade_date_obj)
        except Exception as e:
            errors.append(f"{provider}: {type(e).__name__}: {e}")
            log.warning("bars from %s failed for %s %s: %s", provider, symbol, trade_date_obj, e)
            continue
        if _has_trade_day(df, trade_date_obj):
            if errors:
                log.info("bars for %s %s came from %s after: %s", symbol, trade_date_obj, provider, " | ".join(errors))
            return df
        errors.append(f"{provider}: has {len(df)} bars for {symbol} but none on {trade_date_obj} (latest {df.index.max():%Y-%m-%d %H:%M} ET)")
        log.warning("%s", errors[-1])
        last_df = df
    # Nothing had the trade day. If a provider returned older bars, hand those
    # back so fetch_bars() raises its detailed "no bars in window" message;
    # otherwise raise everything we learned.
    if last_df is not None:
        return last_df
    raise ValueError("No minute bars available -- " + " || ".join(errors))


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

    df = _fetch_raw_bars(symbol, trade_date_obj)
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
        # Say WHAT Polygon actually gave us, so "no data for that day at all"
        # (plan/delay/key problem) is distinguishable from "data exists but
        # not at these times" (wrong timezone or a thin ticker).
        day_df = df[df.index.date == trade_date_obj]
        if day_df.empty:
            detail = (f"the data provider returned {len(df)} bars for {symbol} but NONE on {trade_date} "
                      f"(latest bar it has: {df.index.max():%Y-%m-%d %H:%M} ET) -- the data for that day "
                      f"isn't available to this API key/plan yet")
        else:
            detail = (f"the data provider has {len(day_df)} bars for {symbol} on {trade_date}, from "
                      f"{day_df.index.min():%H:%M} to {day_df.index.max():%H:%M} ET, but none inside the "
                      f"{window_start:%H:%M}-{window_end:%H:%M} window -- check the CSV times are US/Eastern")
        raise ValueError(f"No bars in display window for {symbol}: {detail}")

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

# Daily bars are used both for the 30d-avg-volume/rvol stat on the normal
# /generate-chart call AND for the optional /generate-daily-chart
# support/resistance button. Same FIFO-capped-cache pattern as _bars_cache.
_DAILY_BARS_CACHE_MAX = 200

_daily_bars_cache = {}

_daily_bars_cache_lock = threading.Lock()

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

# Float (share count) practically never changes -- cache it per symbol for
# the life of the process, no eviction needed at any realistic symbol count.
_float_cache = {}

_float_cache_lock = threading.Lock()

# Real trade-print cache, keyed by (symbol, start_iso, end_iso) -- windows
# are short (usually well under a minute, driven by the quiz's checkpoint/
# entry bars) and get replayed often within one quiz session, so this
# avoids re-hitting Polygon every time someone re-plays or reopens a
# question. Same small-FIFO-cache shape as _bars_cache/_float_cache.
_TICKS_CACHE_MAX = 500

_ticks_cache = {}

_ticks_cache_lock = threading.Lock()

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
    # Same Close series, same ewm() call shape as EMA9/EMA20 above -- free to
    # add alongside them, no extra data fetch or API call required.
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()

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
            "ema200": round(float(row["EMA200"]), 4),
            "macd": round(float(row["MACD"]), 4),
            "macd_signal": round(float(row["MACD_signal"]), 4),
            "macd_hist": round(float(row["MACD_hist"]), 4),
        })
    return out

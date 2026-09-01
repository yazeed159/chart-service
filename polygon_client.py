"""
polygon_client.py
Standalone Polygon.io data access for the backtester. No Flask, no n8n --
just the two things a backtest needs:

  1. find_top_gainers(date, top_n)   -- scan the whole US equities tape for
     one trading day and return the top-N overnight gappers.
  2. fetch_minute_bars(symbol, date) -- 1-minute OHLCV bars for one symbol
     on one trading day (regular session + a little pre/post padding).

Rate limiting mirrors chart_service.py's approach (this project's existing
live pipeline): a simple batch limiter tuned for Polygon's free tier (5
calls/minute). If you're on a paid plan, raise POLYGON_BATCH_SIZE /lower
POLYGON_BATCH_WINDOW_S via env vars and this will go faster automatically.

Env vars:
  POLYGON_API_KEY          - required
  POLYGON_BATCH_SIZE       - calls allowed per batch window (default 5)
  POLYGON_BATCH_WINDOW_S   - seconds per batch window (default 65)
  POLYGON_MIN_GAP_S        - minimum stagger between calls in a batch (default 1.5)
"""

import os
import time
import logging
import threading
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

import requests
import pandas as pd

log = logging.getLogger("backtest.polygon")

ET = ZoneInfo("America/New_York")

POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY")
BATCH_SIZE = int(os.environ.get("POLYGON_BATCH_SIZE", 5))
BATCH_WINDOW_S = float(os.environ.get("POLYGON_BATCH_WINDOW_S", 65))
MIN_GAP_S = float(os.environ.get("POLYGON_MIN_GAP_S", 1.5))


class _BatchLimiter:
    """'N calls, then wait, then next N' pacing -- same pattern as the
    existing chart_service.py, tuned for whatever plan you're on."""

    def __init__(self, batch_size: int, window_s: float, min_gap_s: float):
        self._batch_size = batch_size
        self._window_s = window_s
        self._min_gap_s = min_gap_s
        self._lock = threading.Lock()
        self._count = 0
        self._window_start = None
        self._last_call = None

    def wait_turn(self):
        # Sleep OUTSIDE the lock -- see the identical fix/comment on
        # chart_service.py's _PolygonBatchLimiter.wait_turn(). Sleeping
        # while holding self._lock serialized every waiting thread behind
        # whichever one was already sleeping (up to the full 65s window),
        # which is a big deal here since a backtest scan can have many
        # threads/symbols wanting a turn at once.
        while True:
            sleep_for = 0.0
            with self._lock:
                now = time.monotonic()
                if self._window_start is None:
                    self._window_start = now
                if self._count >= self._batch_size:
                    remaining = self._window_s - (now - self._window_start)
                    if remaining > 0:
                        sleep_for = remaining
                    else:
                        self._count = 0
                        self._window_start = now
                        self._last_call = None
                if sleep_for == 0.0 and self._last_call is not None:
                    gap = self._min_gap_s - (now - self._last_call)
                    if gap > 0:
                        sleep_for = gap
                if sleep_for == 0.0:
                    self._count += 1
                    self._last_call = now
                    return
            log.info("Rate limit: waiting %.1fs for next turn", sleep_for)
            time.sleep(sleep_for)


_limiter = _BatchLimiter(BATCH_SIZE, BATCH_WINDOW_S, MIN_GAP_S)


def _require_key():
    if not POLYGON_API_KEY:
        raise RuntimeError("POLYGON_API_KEY environment variable is not set")


def _get(url: str, params: dict, max_attempts: int = 4) -> dict:
    """GET with the shared rate limiter and 429 backoff."""
    for attempt in range(1, max_attempts + 1):
        _limiter.wait_turn()
        resp = requests.get(url, params=params, timeout=20)
        if resp.status_code == 429:
            wait_s = min(2 ** attempt * 3, 30)
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait_s = min(float(retry_after), 30)
                except ValueError:
                    pass
            log.warning("Polygon 429 (attempt %d/%d) -- waiting %.1fs", attempt, max_attempts, wait_s)
            if attempt == max_attempts:
                resp.raise_for_status()
            time.sleep(wait_s)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")


def is_weekday(d: date) -> bool:
    return d.weekday() < 5  # Mon-Fri; US market holidays are handled by grouped-bars returning no results


def trading_days_between(start: date, end: date):
    """Yield calendar weekdays between start and end inclusive. Holidays are
    skipped naturally when find_top_gainers finds no data for that date."""
    d = start
    while d <= end:
        if is_weekday(d):
            yield d
        d += timedelta(days=1)


_grouped_cache: dict = {}


def _fetch_grouped_daily(d: date) -> pd.DataFrame:
    """One row per US ticker for trading day d: columns T, o, h, l, c, v.
    Returns empty DataFrame on market holidays/weekends (Polygon just
    returns no results rather than an error)."""
    _require_key()
    if d in _grouped_cache:
        return _grouped_cache[d]

    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{d.isoformat()}"
    params = {"adjusted": "true", "apiKey": POLYGON_API_KEY}
    payload = _get(url, params)
    results = payload.get("results") or []
    df = pd.DataFrame(results)
    if not df.empty:
        df = df.rename(columns={"T": "symbol", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        df = df[["symbol", "open", "high", "low", "close", "volume"]].set_index("symbol")
    _grouped_cache[d] = df
    return df


def find_top_gainers(d: date, top_n: int = 5, min_price: float = 1.0, max_price: float = 50.0,
                      min_dollar_volume: float = 5_000_000, min_gap_pct: float = 5.0) -> pd.DataFrame:
    """
    Top-N overnight gappers for trading day d, by (open - prior_close)/prior_close.

    Filters (tunable -- these are just sane defaults for a momentum/gap
    strategy, not "the right" thresholds):
      - min_price / max_price: today's open must fall in this band (avoids
        both sub-penny junk and mega-caps that rarely move like momentum names)
      - min_dollar_volume: today's open * volume must clear this, so you're
        not backtesting fills you couldn't realistically have gotten
      - min_gap_pct: must have gapped up at least this much to even qualify

    Returns a DataFrame indexed by symbol with columns: open, prior_close,
    gap_pct, volume -- sorted descending by gap_pct, capped at top_n rows.
    Empty DataFrame if d has no market data (weekend/holiday) or nothing
    clears the filters.
    """
    today = _fetch_grouped_daily(d)
    if today.empty:
        return pd.DataFrame(columns=["open", "prior_close", "gap_pct", "volume"])

    prior_day = d - timedelta(days=1)
    prior = pd.DataFrame()
    tries = 0
    while prior.empty and tries < 5:  # walk back over a weekend/holiday
        prior = _fetch_grouped_daily(prior_day)
        prior_day -= timedelta(days=1)
        tries += 1
    if prior.empty:
        raise RuntimeError(f"Could not find a prior trading day with data before {d}")

    merged = today.join(prior[["close"]].rename(columns={"close": "prior_close"}), how="inner")
    merged = merged[merged["prior_close"] > 0]
    merged["gap_pct"] = (merged["open"] - merged["prior_close"]) / merged["prior_close"] * 100.0
    merged["dollar_volume"] = merged["open"] * merged["volume"]

    filtered = merged[
        (merged["open"] >= min_price)
        & (merged["open"] <= max_price)
        & (merged["dollar_volume"] >= min_dollar_volume)
        & (merged["gap_pct"] >= min_gap_pct)
    ]

    top = filtered.sort_values("gap_pct", ascending=False).head(top_n)
    return top[["open", "prior_close", "gap_pct", "volume"]]


_bars_cache: dict = {}


def fetch_minute_bars(symbol: str, d: date) -> pd.DataFrame:
    """1-minute OHLCV bars for symbol on trading day d, full session
    (pre-market through after-hours as Polygon reports it). Index is
    tz-aware America/New_York timestamps. Empty DataFrame if nothing traded."""
    _require_key()
    cache_key = (symbol, d)
    if cache_key in _bars_cache:
        return _bars_cache[cache_key]

    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/minute/{d.isoformat()}/{d.isoformat()}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": POLYGON_API_KEY}

    all_bars = []
    next_url = url
    page = 0
    while next_url:
        page += 1
        if page > 10:
            raise RuntimeError(f"Too many pages fetching {symbol} {d}")
        payload = _get(next_url, params if next_url == url else {})
        all_bars.extend(payload.get("results") or [])
        next_url = payload.get("next_url")
        if next_url:
            next_url = f"{next_url}&apiKey={POLYGON_API_KEY}"

    if not all_bars:
        df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        _bars_cache[cache_key] = df
        return df

    df = pd.DataFrame(all_bars)
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(ET)
    df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    df = df.set_index("t")[["Open", "High", "Low", "Close", "Volume"]].sort_index()
    _bars_cache[cache_key] = df
    return df

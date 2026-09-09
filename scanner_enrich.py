"""
scanner_enrich.py
Adds the "Arcane Grimoire"-style per-symbol context (float, RVol, VWAP,
EMA9/EMA20/EMA200, day high, and a few threshold-based badges) on top of
scanner.py's bare gap%/price/volume rows, for web-service's /scanner.html.

WHY THIS ISN'T JUST compute_volume_float_stats() / get_full_day_bars()
REUSED DIRECTLY
--------------------------------------------------------------------------
Those two already do the hard part (VWAP/EMA/MACD, 30d avg-volume/RVol) --
this module leans on them for the actual math. But both are wired through
chart_service.py's _bars_cache, which is a permanent, no-TTL,
process-lifetime cache keyed by (symbol, date) -- correct for a PAST
trade date (bars for a closed day never change), wrong for TODAY during
a live premarket scan (price/volume are still moving). So this module
fetches today's bars itself, on a short TTL, instead of going anywhere
near _bars_cache.

WHY A BACKGROUND LOOP INSTEAD OF ENRICHING INSIDE THE /gappers REQUEST
--------------------------------------------------------------------------
Polygon's free tier allows 5 calls/minute (see chart_service.py's shared
_polygon_limiter -- this module reuses that exact limiter, so scanner
enrichment and trade-chart generation share one call budget and can't
blow past the plan together). Enriching N symbols inline in a request
would mean the browser's fetch blocks for however long the limiter makes
the LAST of those N calls wait -- potentially over a minute. Instead a
daemon thread refreshes a small in-memory cache continuously in the
background; /gappers just reads whatever's cached (possibly still empty
for a symbol in the first ~15-60s after it first appears in the top
list -- the frontend shows those fields as "—" until they land, same
spirit as the stale-row fade the page already does for scan age).

SCOPE: only the top SCANNER_ENRICH_MAX_ROWS symbols (by gap_pct) get
enriched at all -- enriching all 50 rows the raw scan can return would
need 50 calls per refresh cycle, which at 5/min is a 10-minute-plus
cycle time, uselessly stale for a live scanner. Fewer, fresher rows beat
more, staler ones.

Env vars (reuses chart-service's existing POLYGON_API_KEY/_polygon_limiter,
no new Polygon credentials needed):
  SCANNER_ENRICH_MAX_ROWS   - default 15
  SCANNER_ENRICH_TTL_S      - default 90 (a cached row older than this is
                                re-fetched on the next loop pass)
  SCANNER_ENRICH_LOOP_S     - default 15 (how often the loop wakes up to
                                check for stale/missing rows)
"""

from __future__ import annotations

import os
import logging
import threading
import time
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

log = logging.getLogger("chart_service.scanner_enrich")

ET = ZoneInfo("America/New_York")
SESSION_START = dtime(4, 0)
SESSION_END = dtime(9, 30)

MAX_ROWS = int(os.environ.get("SCANNER_ENRICH_MAX_ROWS", 15))
TTL_S = float(os.environ.get("SCANNER_ENRICH_TTL_S", 90))
LOOP_S = float(os.environ.get("SCANNER_ENRICH_LOOP_S", 15))

# {symbol: {"data": {...enrichment fields...}, "computed_at": monotonic}}
_cache: dict = {}
_cache_lock = threading.Lock()

_started = False
_start_lock = threading.Lock()


def _low_float_threshold() -> float:
    # Mirrors chart_service.classify_float's own "low" tier boundary, kept
    # as a separate constant here (rather than importing that function's
    # internal number) so a badge threshold can be tuned independently of
    # the journal's float-size tag buckets later without touching this file.
    return 20_000_000


def _extended_pct_threshold() -> float:
    return 8.0  # % away from EMA9 before flagging "Extended"


def _compute_badges(last_close, vwap, ema9, float_shares, relative_volume) -> list[str]:
    badges = []
    if float_shares and float_shares < _low_float_threshold():
        badges.append("Low Float")
    if vwap:
        badges.append("Above VWAP" if last_close >= vwap else "Below VWAP")
    if ema9 and ema9 > 0:
        pct_from_ema9 = abs(last_close - ema9) / ema9 * 100.0
        if pct_from_ema9 >= _extended_pct_threshold():
            badges.append("Extended From EMA9")
    if relative_volume is not None and relative_volume >= 5:
        badges.append("High RVol")
    return badges


def _enrich_one(symbol: str, cs) -> dict | None:
    """cs = the chart_service module, passed in rather than imported at
    module load time -- chart_service.py imports THIS module, so importing
    it back here at load time would be a circular import. Returns None on
    any failure (missing data, Polygon error) -- enrichment is always
    best-effort, never allowed to break the plain /gappers response."""
    today = datetime.now(ET).date()
    try:
        raw = cs._fetch_raw_bars_from_polygon(symbol, today)  # noqa: SLF001 -- see module docstring
        if raw.empty:
            return None
        with_ind = cs.compute_indicators(raw)
        session_only = with_ind[with_ind.index.date == today]
        if session_only.empty:
            return None
        latest = session_only.iloc[-1]

        vol_stats = cs.compute_volume_float_stats(symbol, today, raw)
        try:
            float_shares = cs._fetch_float_shares(symbol)  # noqa: SLF001
        except Exception as e:
            log.warning("Float lookup failed for %s during scanner enrich (non-fatal): %s", symbol, e)
            float_shares = None

        last_close = float(latest["Close"])
        vwap = float(latest["VWAP"]) if latest["VWAP"] == latest["VWAP"] else None  # NaN check
        ema9 = float(latest["EMA9"]) if latest["EMA9"] == latest["EMA9"] else None
        ema20 = float(latest["EMA20"]) if latest["EMA20"] == latest["EMA20"] else None
        ema200 = float(latest["EMA200"]) if latest["EMA200"] == latest["EMA200"] else None
        day_high = float(session_only["High"].max())

        return {
            "day_high": round(day_high, 4),
            "vwap": round(vwap, 4) if vwap is not None else None,
            "ema9": round(ema9, 4) if ema9 is not None else None,
            "ema20": round(ema20, 4) if ema20 is not None else None,
            "ema200": round(ema200, 4) if ema200 is not None else None,
            "float_shares": float_shares,
            "float_tag": cs.classify_float(float_shares),
            "avg_volume_30d": vol_stats.get("avg_volume_30d"),
            "relative_volume": vol_stats.get("relative_volume"),
            "rvol_tag": vol_stats.get("rvol_tag", "rvol_unknown"),
            "badges": _compute_badges(last_close, vwap, ema9, float_shares, vol_stats.get("relative_volume")),
        }
    except Exception:
        log.exception("Scanner enrichment failed for %s", symbol)
        return None


def _loop(cs, gappers_store):
    while True:
        try:
            now_et = datetime.now(ET)
            in_session = now_et.weekday() < 5 and SESSION_START <= now_et.time() < SESSION_END
            if in_session:
                rows = gappers_store.list_todays_gappers(limit=MAX_ROWS)
                for row in rows:
                    symbol = row["symbol"]
                    with _cache_lock:
                        cached = _cache.get(symbol)
                    if cached and (time.monotonic() - cached["computed_at"]) < TTL_S:
                        continue
                    data = _enrich_one(symbol, cs)
                    if data is not None:
                        with _cache_lock:
                            _cache[symbol] = {"data": data, "computed_at": time.monotonic()}
            else:
                # Outside the session, don't spend Polygon calls keeping a
                # scan refreshed that isn't running -- see scanner.py's own
                # identical session-window reasoning.
                pass
        except Exception:
            log.exception("Scanner enrichment loop iteration failed -- continuing")
        time.sleep(LOOP_S)


def start(cs, gappers_store):
    """Idempotent -- call this from chart_service.py once, at import time,
    near the /gappers route. Safe to call more than once (e.g. under a
    dev-server reloader); only the first call actually spawns the thread."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(cs, gappers_store), daemon=True, name="scanner-enrich").start()
    log.info("Scanner enrichment loop started (max_rows=%d, ttl=%.0fs, loop=%.0fs)", MAX_ROWS, TTL_S, LOOP_S)


def get_enrichment(symbol: str) -> dict | None:
    with _cache_lock:
        cached = _cache.get(symbol)
    return cached["data"] if cached else None

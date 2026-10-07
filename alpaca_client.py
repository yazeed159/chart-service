"""
alpaca_client.py
Minimal wrapper around Alpaca's market-data snapshot endpoint -- the one
thing scanner.py needs that Polygon's free tier can't give (real-time
prices; Polygon free is 15-min delayed, see polygon_client.py). No SDK
dependency, just `requests`, matching the plain-requests style everywhere
else in this repo.

Free-tier facts this module assumes (see scanner.py's module docstring
for the fuller writeup of the tradeoffs):
  - feed="iex" is the only feed the free plan can read. Real-time, but
    IEX-only trades/volume (~2-3% of consolidated market volume) --
    prices are accurate, volume/RVOL numbers are a relative proxy, not
    the true tape-wide total.
  - Free-plan rate limit is 200 calls/min. A single /v2/stocks/snapshots
    call accepts thousands of symbols, so scanner.py's whole ~1-2k
    candidate universe fits in ONE call per poll cycle -- nowhere close
    to the rate limit even polling every 15-20s.

Env vars:
  ALPACA_API_KEY_ID      - required
  ALPACA_API_SECRET_KEY  - required
  ALPACA_DATA_FEED        - default "iex" (free tier). Only override to
                             "sip" if you've actually upgraded to a paid
                             market-data plan -- "sip" on a free-tier key
                             just 403s. (Applies to the real-time snapshot
                             calls only -- see get_minute_bars below.)
  ALPACA_BARS_FEED        - feed for HISTORICAL minute bars (chart data).
                             Default "sip": on the free plan the full-market
                             SIP feed IS allowed for any bar whose window ends
                             at least 15 minutes ago, so charts for yesterday's
                             premarket get consolidated-tape bars, not the
                             ~2-3% IEX-only subset.
"""

from __future__ import annotations

import os
import logging

import requests

log = logging.getLogger("chart_service.alpaca")

DATA_BASE_URL = "https://data.alpaca.markets/v2"
API_KEY_ID = os.environ.get("ALPACA_API_KEY_ID", "")
API_SECRET_KEY = os.environ.get("ALPACA_API_SECRET_KEY", "")
DATA_FEED = os.environ.get("ALPACA_DATA_FEED", "iex")

# Comfortably under the documented ~4000-symbol ceiling per call, and
# still just 1-2 calls for a ~1-2k universe -- see build_candidate_universe.
CHUNK_SIZE = 1500


def _require_key():
    if not API_KEY_ID or not API_SECRET_KEY:
        raise RuntimeError("ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY are not set")


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID": API_KEY_ID,
        "APCA-API-SECRET-KEY": API_SECRET_KEY,
    }


def get_snapshots(symbols: list[str]) -> dict[str, dict]:
    """Latest trade + previous daily close for each symbol, chunked to
    stay well under the per-call symbol ceiling. Returns {symbol: snapshot
    dict}; symbols Alpaca doesn't recognize are just absent from the
    result rather than failing the whole batch (each chunk request still
    fails hard on a genuinely malformed symbol -- see the module-level
    note in scanner.py about filtering the universe to plain equity
    tickers before this is ever called).
    """
    _require_key()
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), CHUNK_SIZE):
        chunk = symbols[i:i + CHUNK_SIZE]
        resp = requests.get(
            f"{DATA_BASE_URL}/stocks/snapshots",
            headers=_headers(),
            params={"symbols": ",".join(chunk), "feed": DATA_FEED},
            timeout=20,
        )
        if resp.status_code >= 300:
            log.error("Alpaca snapshots chunk (%d symbols) failed: %s %s",
                       len(chunk), resp.status_code, resp.text[:500])
            resp.raise_for_status()
        out.update(resp.json() or {})
    return out


BARS_FEED = os.environ.get("ALPACA_BARS_FEED", "sip")


def available() -> bool:
    return bool(API_KEY_ID and API_SECRET_KEY)


def get_minute_bars(symbol: str, start_utc, end_utc) -> list[dict]:
    """Historical 1-minute bars (pre-market through after-hours) for one
    symbol, split-adjusted, oldest first. start_utc/end_utc are tz-aware
    datetimes. On the free plan `end_utc` must be >= 15 minutes in the past
    for feed=sip (caller clamps it). Pages through next_page_token. Raises
    on HTTP errors with Alpaca's own message so it can be shown to the user.
    Returns [] when Alpaca simply has no bars in that range."""
    import time as _time
    _require_key()
    bars: list[dict] = []
    params = {
        "timeframe": "1Min",
        "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 10000, "adjustment": "split", "feed": BARS_FEED, "sort": "asc",
    }
    for _page in range(20):
        for attempt in range(3):
            resp = requests.get(
                f"{DATA_BASE_URL}/stocks/{symbol}/bars",
                headers=_headers(), params=params, timeout=20,
            )
            if resp.status_code == 429 and attempt < 2:
                _time.sleep(2 * (attempt + 1))
                continue
            break
        if resp.status_code >= 300:
            msg = resp.text[:300]
            log.error("Alpaca bars %s failed: %s %s", symbol, resp.status_code, msg)
            raise RuntimeError(f"Alpaca bars HTTP {resp.status_code}: {msg}")
        body = resp.json() or {}
        bars.extend(body.get("bars") or [])
        token = body.get("next_page_token")
        if not token:
            return bars
        params["page_token"] = token
    raise RuntimeError(f"Alpaca bars for {symbol}: more than 20 pages -- aborting")

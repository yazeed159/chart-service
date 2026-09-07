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
                             just 403s.
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

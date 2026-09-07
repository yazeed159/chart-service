"""
scanner.py
The live premarket "top_gappers" scanner -- the piece that was missing
for a top_gappers strategy_rule to actually work live (see
strategy_store.py and live-service/engine.py's start_run docstrings).
Writes today's qualifying gappers to Supabase via gappers_store.py;
live-service reads them back through gappers_store.get_symbols_for_rule().

--------------------------------------------------------------------------
WHY THIS ISN'T PART OF chart_service.py's WEB SERVICE
--------------------------------------------------------------------------
This needs to poll continuously for the ~5.5 hours of the premarket
session (4:00-9:30 ET), not respond to a request. Bundling that into the
always-on Flask process would mean either blocking a worker for 5.5
hours or juggling a background thread that has to survive redeploys --
messier than it needs to be. Instead this is a standalone script, run as
its OWN Render Cron Job (separate service, same repo -- start command
`python scanner.py`, schedule `55 8 * * 1-5` UTC, i.e. 3:55 AM ET
weekdays, since cron time is UTC and ET is UTC-5 or UTC-4 depending on
DST -- adjust if you want to be precise across the DST boundary, a few
minutes early/late doesn't matter much). It runs until 9:30 ET, then
exits on its own, which is exactly what a cron job is for -- no
Background Worker (paid tier) needed.

--------------------------------------------------------------------------
WHY PREMARKET ONLY, AND WHY THIS STAYS ON ALPACA'S FREE PLAN
--------------------------------------------------------------------------
By design, per current instructions -- this only covers the classic
4:00-9:30 ET premarket gap session. Extending into regular hours,
after-hours, or the 8PM-4AM overnight session is a real option later, but
changes both what "gap" means (reference close flips per session -- see
the conversation this was scoped in) and the deployment (a longer window
needs a paid Render Background Worker, not a free Cron Job; overnight
data specifically needs Alpaca's paid Algo Trader Plus plan for the
`boats` feed). Revisit if/when that's wanted.

--------------------------------------------------------------------------
DATA FLOW
--------------------------------------------------------------------------
1. Build today's candidate universe ONCE at startup: yesterday's (or the
   last trading day's) close/volume via polygon_client.build_candidate_
   universe -- a single Polygon grouped-daily call, free-tier-friendly
   since it's one call, not per-symbol.
2. Poll that universe every POLL_INTERVAL_S via alpaca_client.get_snapshots
   (real-time IEX, unlike Polygon's free 15-min-delayed feed -- this is
   the actual reason Alpaca is in the mix at all).
3. Compute gap_pct = (latest trade price - previous daily close) /
   previous daily close, filter to a generous default band (tighter
   per-strategy filtering happens at read time in
   gappers_store.get_symbols_for_rule, against whatever thresholds that
   strategy's symbol_rule actually specifies), upsert the top N.

Env vars (new, set on the Cron Job service specifically -- it does NOT
inherit chart-service's web service env vars just because it's the same
repo):
  POLYGON_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY  - reused
  ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY                  - new
  SCANNER_POLL_INTERVAL_S   - default 20
  SCANNER_MIN_PRICE / SCANNER_MAX_PRICE               - default 1 / 50
  SCANNER_MIN_DOLLAR_VOLUME                            - default 1_000_000
    (universe-building threshold -- deliberately looser than a strategy's
    own min_dollar_volume, which is applied again at read time; this one
    just keeps the candidate list from being enormous)
  SCANNER_MAX_UNIVERSE       - default 1500
  SCANNER_TOP_N               - default 50 (rows written per cycle; a
    strategy's own top_n at read time trims further)
"""

from __future__ import annotations

import os
import time
import logging
from datetime import date, timedelta, datetime, time as dtime
from zoneinfo import ZoneInfo

import polygon_client
import alpaca_client
import gappers_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scanner")

ET = ZoneInfo("America/New_York")

POLL_INTERVAL_S = float(os.environ.get("SCANNER_POLL_INTERVAL_S", 20))
MIN_PRICE = float(os.environ.get("SCANNER_MIN_PRICE", 1.0))
MAX_PRICE = float(os.environ.get("SCANNER_MAX_PRICE", 50.0))
MIN_DOLLAR_VOLUME = float(os.environ.get("SCANNER_MIN_DOLLAR_VOLUME", 1_000_000))
MAX_UNIVERSE = int(os.environ.get("SCANNER_MAX_UNIVERSE", 1500))
TOP_N = int(os.environ.get("SCANNER_TOP_N", 50))

SESSION_START = dtime(4, 0)   # premarket open, ET
SESSION_END = dtime(9, 30)    # regular-session open, ET -- see module docstring


def _build_universe(today: date) -> list[str]:
    d = today - timedelta(days=1)
    tries = 0
    while tries < 5:
        universe = polygon_client.build_candidate_universe(
            d, min_price=MIN_PRICE, max_price=MAX_PRICE,
            min_dollar_volume=MIN_DOLLAR_VOLUME, max_symbols=MAX_UNIVERSE,
        )
        if universe:
            log.info("Candidate universe: %d symbols (from %s close)", len(universe), d)
            return universe
        d -= timedelta(days=1)  # walk back over a weekend/holiday
        tries += 1
    raise RuntimeError(f"Could not build a candidate universe before {today}")


def _poll_once(today: date, universe: list[str]):
    snapshots = alpaca_client.get_snapshots(universe)
    rows = []
    for symbol, snap in snapshots.items():
        trade = snap.get("latestTrade") or {}
        prev = snap.get("prevDailyBar") or {}
        price = trade.get("p")
        prior_close = prev.get("c")
        if not price or not prior_close:
            continue  # no premarket print yet, or prior bar missing -- skip, not an error
        gap_pct = (price - prior_close) / prior_close * 100.0
        if gap_pct < 3.0:  # loose floor, real filtering happens per-strategy at read time
            continue
        daily_bar = snap.get("dailyBar") or {}
        rows.append({
            "symbol": symbol,
            "price": price,
            "gap_pct": round(gap_pct, 2),
            "premkt_volume": daily_bar.get("v", 0),
        })
    rows.sort(key=lambda r: r["gap_pct"], reverse=True)
    rows = rows[:TOP_N]
    if rows:
        gappers_store.upsert_gappers(today, rows)
    log.info("Poll: %d/%d symbols qualified, top written: %s",
              len(rows), len(universe), [r["symbol"] for r in rows[:5]])


def run():
    now = datetime.now(ET)
    today = now.date()
    if today.weekday() >= 5:
        log.info("Weekend, nothing to do.")
        return
    session_end_dt = datetime.combine(today, SESSION_END, tzinfo=ET)
    if now.time() >= SESSION_END:
        log.info("Started after %s ET session end -- nothing to do today.", SESSION_END)
        return

    universe = _build_universe(today)

    while True:
        now = datetime.now(ET)
        if now >= session_end_dt:
            log.info("Reached %s ET -- session over, exiting.", SESSION_END)
            return
        cycle_start = time.monotonic()
        try:
            _poll_once(today, universe)
        except Exception:
            log.exception("Poll cycle failed -- continuing to next cycle")
        elapsed = time.monotonic() - cycle_start
        time.sleep(max(0.0, POLL_INTERVAL_S - elapsed))


if __name__ == "__main__":
    run()

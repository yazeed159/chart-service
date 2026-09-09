"""
scanner.py
The live premarket "top_gappers" scanner -- the piece that was missing
for a top_gappers strategy_rule to actually work live (see
strategy_store.py and live-service/engine.py's start_run docstrings).
Writes today's qualifying gappers to Supabase via gappers_store.py;
live-service reads them back through gappers_store.get_symbols_for_rule().

--------------------------------------------------------------------------
UPDATE: RUNS AS AN IN-PROCESS BACKGROUND THREAD, NOT A SEPARATE CRON JOB
--------------------------------------------------------------------------
Originally this was meant to run as its own Render Cron Job (see git
history) so the ~5.5 hour premarket poll wouldn't block a Flask worker.
Render Cron Jobs are not on the free tier though (billed per minute, ~$1/mo
minimum even for a light job), so this now runs the same way
scanner_enrich.py already does: a daemon thread started once from
chart_service.py at import time (see start(), same shape as
scanner_enrich.start()), living inside chart-service's existing free web
service. No second paid service needed.

The tradeoff this reintroduces (that a separate Cron Job would have
avoided): a free Render web service spins down after ~15 min with no
inbound HTTP traffic, which kills this thread along with the rest of the
process. A redeploy also restarts the thread from scratch (harmless --
_build_universe just reruns). To keep the service alive through the
4:00-9:30 ET premarket window, point a free uptime pinger (e.g.
cron-job.org or UptimeRobot, both free) at chart-service's GET /health
every 5-10 minutes. That's the free-tier price of not paying for a Cron
Job: an external, zero-cost keepalive instead.

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

Env vars (set on chart-service itself, same service as everything else in
this file's docstring history -- no separate service to configure now):
  POLYGON_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY  - already set
    for chart-service's other routes, reused here as-is
  ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY                  - new, add
    these to chart-service's existing env vars (free Alpaca account, IEX
    feed -- see alpaca_client.py)
  SCANNER_POLL_INTERVAL_S   - default 5. Well within Alpaca's free-tier
    200 calls/min (~1-2 calls per cycle -- this could go lower still, but
    5s is already close to the floor of what matters: the IEX feed itself
    isn't tick-by-tick guaranteed faster than that, and live-service's own
    re-check (GAPPERS_RUN_POLL_S, engine.py) is the other half of the
    latency budget)
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
import threading
from datetime import date, timedelta, datetime, time as dtime
from zoneinfo import ZoneInfo

import polygon_client
import alpaca_client
import gappers_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scanner")

ET = ZoneInfo("America/New_York")

POLL_INTERVAL_S = float(os.environ.get("SCANNER_POLL_INTERVAL_S", 5))
MIN_PRICE = float(os.environ.get("SCANNER_MIN_PRICE", 1.0))
MAX_PRICE = float(os.environ.get("SCANNER_MAX_PRICE", 50.0))
MIN_DOLLAR_VOLUME = float(os.environ.get("SCANNER_MIN_DOLLAR_VOLUME", 1_000_000))
MAX_UNIVERSE = int(os.environ.get("SCANNER_MAX_UNIVERSE", 1500))
TOP_N = int(os.environ.get("SCANNER_TOP_N", 50))

SESSION_START = dtime(4, 0)   # premarket open, ET
SESSION_END = dtime(9, 30)    # regular-session open, ET -- see module docstring

IDLE_CHECK_S = 60  # how often the thread wakes up to recheck the session
                    # window while outside it -- cheap, no API calls made


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


_started = False
_start_lock = threading.Lock()

# Cache the day's candidate universe so re-entering the session window
# (e.g. after a Render redeploy mid-morning) doesn't rebuild it from
# scratch every time -- built once per calendar day, on first use.
_universe_cache_date: date | None = None
_universe_cache: list[str] = []


def _get_universe(today: date) -> list[str]:
    global _universe_cache_date, _universe_cache
    if _universe_cache_date != today:
        _universe_cache = _build_universe(today)
        _universe_cache_date = today
    return _universe_cache


def _loop():
    """Runs forever as a daemon thread (see start()). Unlike the old
    standalone-script run(), this never exits -- it just idles at
    IDLE_CHECK_S outside the session window and resumes polling the next
    time SESSION_START-SESSION_END rolls around, so one thread covers
    every trading day the process happens to be alive for."""
    while True:
        sleep_s = IDLE_CHECK_S
        try:
            now = datetime.now(ET)
            today = now.date()
            in_session = today.weekday() < 5 and SESSION_START <= now.time() < SESSION_END
            if in_session:
                cycle_start = time.monotonic()
                universe = _get_universe(today)
                _poll_once(today, universe)
                elapsed = time.monotonic() - cycle_start
                sleep_s = max(0.0, POLL_INTERVAL_S - elapsed)
        except Exception:
            log.exception("Scanner loop iteration failed -- continuing")
        time.sleep(sleep_s)


def start():
    """Idempotent -- call this from chart_service.py once, at import time
    (same pattern as scanner_enrich.start()). Safe to call more than once
    (e.g. under a dev-server reloader); only the first call spawns the
    thread."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, daemon=True, name="gap-scanner").start()
    log.info("Gap scanner thread started (poll=%.0fs, session=%s-%s ET)",
              POLL_INTERVAL_S, SESSION_START, SESSION_END)


if __name__ == "__main__":
    # Still runnable standalone (e.g. local testing) -- just runs the loop
    # in the foreground instead of as a background thread.
    _loop()

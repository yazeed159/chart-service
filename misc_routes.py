"""
misc_routes.py
/health (uptime pinger target) and /gappers (read-only view of scanner.py's
premarket scan), plus starting the scanner.py / scanner_enrich.py background
threads -- both need to be running for either the pinger or /gappers to be
useful, so their startup lives alongside these two routes. Split out of the
old chart_service.py -- see chart_service.py's module docstring for how
these files fit together.
"""

import sys as _sys
from datetime import datetime, time as dtime

from flask import jsonify, request

import gappers_store
import scanner_enrich
import scanner
import market_data
from config import app, ET, _require_user

@app.route("/health", methods=["GET"])
def health():
    # Deliberately unauthenticated and cheap -- this exists only so a
    # free uptime pinger (cron-job.org, UptimeRobot, etc.) can hit it
    # every few minutes to stop Render's free tier from spinning this
    # service down, which would otherwise kill the scanner.py and
    # scanner_enrich.py background threads along with it.
    return jsonify({"ok": True})


# Starts a background thread (see scanner_enrich.py's module docstring for
# why this can't just enrich inline inside the /gappers request) that
# keeps the top SCANNER_ENRICH_MAX_ROWS gappers' float/RVol/VWAP/EMA/badges
# refreshed, sharing market_data.py's Polygon rate limiter (_polygon_limiter)
# so it can never push total Polygon usage past the plan's actual limit.
# Passed the market_data module (not this one) since that's where
# scanner_enrich.py's cs.compute_indicators / cs._fetch_raw_bars_from_polygon
# / etc. actually live now.
scanner_enrich.start(market_data, gappers_store)

# Starts the actual premarket gap scan itself as a second background
# thread -- see scanner.py's module docstring for why this now runs
# in-process here instead of as a separate (paid) Render Cron Job, and
# why that means chart-service needs to be kept awake through the
# 4:00-9:30 ET window via a free external pinger against GET /health.
scanner.start()


# --- Live gappers: read-only view of scanner.py's premarket scan, for
# web-service's /scanner.html page. Market-wide, not user-scoped (see
# gappers_store.py's docstring) -- every logged-in account sees the same
# rows. Still auth-gated (not a public endpoint) since nothing on this
# site is public.
@app.route("/gappers", methods=["GET", "OPTIONS"])
def gappers_list():
    if request.method == "OPTIONS":
        return "", 204
    _, err = _require_user()
    if err:
        return err
    now_et = datetime.now(ET)
    session_active = now_et.weekday() < 5 and dtime(4, 0) <= now_et.time() < dtime(9, 30)
    rows = gappers_store.list_todays_gappers(limit=50)
    # Enrichment is additive and best-effort: a row whose enrichment
    # hasn't landed yet (or falls past the top SCANNER_ENRICH_MAX_ROWS,
    # which never gets enriched at all -- see scanner_enrich.py) just
    # keeps these fields null; the frontend renders that as "—", not an
    # error.
    for row in rows:
        extra = scanner_enrich.get_enrichment(row["symbol"])
        if extra:
            row.update(extra)
    return jsonify({
        "rows": rows,
        "server_time": now_et.isoformat(),
        "session_active": session_active,
        "enrich_max_rows": scanner_enrich.MAX_ROWS,
    })

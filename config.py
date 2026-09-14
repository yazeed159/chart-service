"""
config.py
Shared Flask app instance, process-wide config/env-vars, and small
cross-domain route helpers for chart_service's split-up modules (market_data,
charts, float_routes, backtest_routes, strategies_routes, misc_routes -- see
chart_service.py's module docstring for how they fit together). Every other
module in this service imports from here rather than from each other
directly, so this file has to stay free of imports from any of them.
"""

import os
import logging
import threading
from datetime import time as dtime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
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

# One request to /tick-data can, at worst, ask for a full 60s bar on a
# heavily-traded ticker -- cap how many individual prints we'll pull back
# and hand to the browser so neither Polygon pagination nor the response
# payload can run away on us.
TICK_DATA_MAX_TRADES = int(os.environ.get("TICK_DATA_MAX_TRADES", 2000))

@app.after_request
def _add_cors_headers(resp):
    # This service is called directly from the browser (backtester.js) as
    # well as from n8n -- CORS only matters for the browser calls, and is a
    # no-op for server-to-server ones, so it's safe to apply to every route.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, ngrok-skip-browser-warning"
    return resp

import re
_JOB_ID_RE = re.compile(r"^[0-9a-fA-F]+$")  # job ids are uuid4().hex[:12] -- reject anything else before touching Supabase

from supabase_auth import resolve_user_id


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

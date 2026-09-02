"""
backtest_import_routes.py
Flask Blueprint replacing n8n's "Backtest Import Webhook Trigger" branch --
the last of the 4 "easy win" site-facing webhooks (SR / Chat / Backtest AI
already live in ai_routes.py). This is the "Send to Journal" button on the
Backtester/Report pages (backtester.js / report.js -> config.js's
window.N8N_BACKTEST_IMPORT_URL).

Register from chart_service.py the same way as ai_routes.py / import_routes.py:

    from backtest_import_routes import bp as backtest_import_bp
    app.register_blueprint(backtest_import_bp)

POST /backtest-import
  { "run": {"label", "source": "backtest", "job_id", "callback_url",
             "started", "ended"},
    "trades": [ {date, symbol, entry_time, entry_price, exit_time,
                 exit_price, exit_reason, shares, pnl_dollars,
                 pnl_dollars_gross, commission_total, r_multiple, win}, ... ] }

  Responds immediately -- { "received": <n> } -- matching n8n's webhook
  responseMode: "onReceived", since report.js already polls
  GET /backtest/history/<job_id>/report afterwards rather than waiting on
  this response. The real work happens in a background thread:

    1. One /generate-chart call per trade (bars + indicators, no verdict --
       a backtest trade already has its final entry/exit from the
       backtester engine, so there's no vision-LLM grading step here,
       mirroring "Route: Backtest Import?" -> "Attach Chart (No Verdict)"
       in the n8n graph, NOT the daily-pipeline path with Gemini). PNG
       rendering is left off (chart_service.py's include_image now
       defaults to false), so chart_image below is null.
    2. Calls _build_chart_response() directly (in-process) instead of an
       HTTP round-trip to this same service's own /generate-chart route.
    3. Paced CHART_PACING_SECONDS apart, matching "Generate Chart
       (Backtest)"'s batching (batchSize=1, batchInterval=13000) so a
       dozen-plus-trade run doesn't blow through the shared Polygon rate
       limiter chart_service.py's other callers also depend on.
    4. Once every trade has been attempted, POSTs the whole batch to
       run.callback_url in one call -- same shape as "Build Backtest
       Callback Body" -> "POST Backtest Callback", and the same contract
       chart_service.py's own POST /backtest/history/<job_id>/enrich
       already expects.

  A trade whose chart render fails is still included in the callback body
  with bars/indicators/chart_image left null, rather than dropped, so one
  bad symbol/date doesn't silently swallow the rest of the run -- it just
  shows up stuck without a chart on the report page instead.

NOT included: FIFO trade-matching (trade_matching.py) or the vision-LLM
verdict step -- neither applies to backtest trades, see above.
"""

import time
import logging
import threading

import requests
from flask import Blueprint, request, jsonify

log = logging.getLogger("chart_service.backtest_import_routes")
bp = Blueprint("backtest_import_routes", __name__)

# Matches n8n's "Generate Chart (Backtest)" node: options.batching.batch =
# { batchSize: 1, batchInterval: 13000 }.
CHART_PACING_SECONDS = 13
CALLBACK_TIMEOUT_S = 30


def _run_backtest_import(run: dict, trades: list):
    """Runs in a background thread -- generates one chart per trade, paced,
    then POSTs the whole enriched batch back to run['callback_url']."""
    # Lazy import so it doesn't matter whether chart_service.py or this
    # module gets imported first (same reasoning as ai_routes.py).
    from chart_service import _build_chart_response

    job_id = run.get("job_id")
    callback_url = run.get("callback_url")
    out_trades = []

    for i, t in enumerate(trades):
        if i > 0:
            time.sleep(CHART_PACING_SECONDS)

        symbol = t.get("symbol")
        date = t.get("date")
        entry_time = t.get("entry_time")
        chart = None
        try:
            chart_body = {
                "symbol": symbol,
                "trade_date": date,
                "entry_time": entry_time,
                "exit_time": t.get("exit_time"),
                "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price"),
                # Backtester is long-only (ORB/gapper-style entries) -- no
                # side field comes through in the trade payload, matches
                # Extract & Match Trades1's backtest branch defaulting to Long.
                "side": "long",
                "include_volume_stats": False,
            }
            chart = _build_chart_response(chart_body, time.monotonic())
        except Exception as e:
            log.error(
                "backtest-import job %s: chart failed for %s %s %s: %s",
                job_id, symbol, date, entry_time, e,
            )

        chart_image = (
            f"data:image/png;base64,{chart['image_base64']}"
            if chart and chart.get("image_base64") else None
        )
        out_trades.append({
            "date": date,
            "symbol": symbol,
            "entry_time": entry_time,
            "verdict": "",
            "bars": chart["bars"] if chart else None,
            "indicators": chart["indicators"] if chart else None,
            "chart_image": chart_image,
            "lessons": [],
            "better_entry_price": None,
            "better_entry_reason": "",
            "better_exit_price": None,
            "better_exit_reason": "",
        })

    if not callback_url:
        log.error(
            "backtest-import job %s: no callback_url on run, dropping %d enriched trades",
            job_id, len(out_trades),
        )
        return

    try:
        resp = requests.post(callback_url, json={"trades": out_trades}, timeout=CALLBACK_TIMEOUT_S)
        resp.raise_for_status()
        log.info("backtest-import job %s: callback delivered for %d trades", job_id, len(out_trades))
    except Exception as e:
        log.error("backtest-import job %s: callback POST failed: %s", job_id, e)


@bp.route("/backtest-import", methods=["POST", "OPTIONS"])
def backtest_import():
    if request.method == "OPTIONS":
        return "", 204

    body = request.get_json(force=True, silent=True) or {}
    run = body.get("run") or {}
    trades = body.get("trades") or []

    if not trades:
        return jsonify({"error": "no trades in request body"}), 400
    if not run.get("callback_url"):
        return jsonify({"error": "run.callback_url is required"}), 400

    th = threading.Thread(target=_run_backtest_import, args=(run, trades), daemon=True)
    th.start()

    log.info("backtest-import job %s: accepted %d trades", run.get("job_id"), len(trades))
    return jsonify({"received": len(trades)}), 200

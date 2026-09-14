"""
float_routes.py
The /fetch-float and /fetch-float/bulk/* routes (on-demand + batch float
lookups). Split out of the old chart_service.py -- see chart_service.py's
module docstring for how these files fit together.
"""

import threading
import uuid

from flask import request, jsonify

import float_shares_store
from config import app, log, _require_user
from market_data import _fetch_float_shares, classify_float

@app.route("/fetch-float", methods=["POST", "OPTIONS"])
def fetch_float():
    """POST {trade_id, symbol} -> {float_shares, float_tag}. Requires
    Authorization: Bearer <supabase JWT> (see _require_user).

    On-demand replacement for the float lookup compute_volume_float_stats
    used to run automatically on every /generate-chart call -- see that
    function's docstring. Wired to a "Get float" button on the trade
    detail page (trade.js), so a live Polygon call for a symbol's float
    now only happens when someone actually asks for it, not for every
    trade that gets published.

    _fetch_float_shares below still only ever costs Polygon one real call
    per symbol, ever, across every trade and every account (see
    float_shares_store.py's in-memory + Supabase caching) -- this route
    doesn't change that. It just moves WHEN that call can get triggered:
    from "automatically, on every trade, whether anyone looks or not" to
    "on request, the first time someone actually wants that symbol's
    float".

    Also merges the result into this one trade's stored
    trade_details.indicators and trades.float_tag in Supabase (see
    float_shares_store.save_float_to_trade), scoped to the calling user,
    so re-opening the same trade later shows the float without another
    round trip here."""
    if request.method == "OPTIONS":
        return "", 204

    user_id, err = _require_user()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    trade_id = (body.get("trade_id") or "").strip()
    symbol = (body.get("symbol") or "").strip().upper()
    if not trade_id or not symbol:
        return jsonify({"error": "trade_id and symbol are required"}), 400

    try:
        shares = _fetch_float_shares(symbol)
    except Exception as e:
        log.warning("On-demand float lookup failed for %s: %s", symbol, e)
        return jsonify({"error": f"Float lookup failed: {e}"}), 502

    float_tag = classify_float(shares)

    try:
        float_shares_store.save_float_to_trade(user_id, trade_id, shares, float_tag)
    except Exception as e:
        # The Polygon lookup already succeeded -- don't fail the request
        # (and cost the user another click/another Polygon round trip
        # next time) just because persisting it hit a snag. Worst case
        # this trade just re-saves next time its "Get float" is clicked.
        log.warning("Saving float for trade %s (%s) failed (non-fatal): %s", trade_id, symbol, e)

    return jsonify({"float_shares": shares, "float_tag": float_tag})

# ---------------------------------------------------------------------------
# Bulk "fill missing floats" job -- same start/poll/cancel shape as the
# backtester job below (_backtest_jobs), just walking every symbol this
# user has a trade on with no float yet instead of every trading day in a
# date range. Wired to a button on the journal page (journal.html) rather
# than a per-trade one, so someone doesn't have to open every trade that
# predates this feature (or got imported in bulk) and click "Get float"
# one at a time.
#
# Runs one live Polygon call per *symbol* (not per trade) -- multiple
# trades on the same symbol share one _fetch_float_shares call, same
# caching/rate-limiting as the single-trade /fetch-float route above --
# then writes the result onto every one of that symbol's trades via
# save_float_to_trade, so each one shows its float next time it's opened
# without needing its own "Get float" click.
# ---------------------------------------------------------------------------
_float_bulk_jobs = {}

_float_bulk_jobs_lock = threading.Lock()

def _run_float_bulk_job(job_id: str, user_id: str, symbol_to_trade_ids: dict):
    def cancelled():
        with _float_bulk_jobs_lock:
            job = _float_bulk_jobs.get(job_id)
            return bool(job and job.get("cancel_requested"))

    symbols = sorted(symbol_to_trade_ids.keys())
    for i, symbol in enumerate(symbols):
        if cancelled():
            with _float_bulk_jobs_lock:
                _float_bulk_jobs[job_id]["status"] = "cancelled"
            return

        with _float_bulk_jobs_lock:
            _float_bulk_jobs[job_id]["current_symbol"] = symbol
            _float_bulk_jobs[job_id]["current_index"] = i  # symbols fully done so far

        trade_ids = symbol_to_trade_ids[symbol]
        try:
            shares = _fetch_float_shares(symbol)
            float_tag = classify_float(shares)
            for trade_id in trade_ids:
                try:
                    float_shares_store.save_float_to_trade(user_id, trade_id, shares, float_tag)
                except Exception as e:
                    log.warning("Bulk float job %s: saving %s (trade %s) failed: %s", job_id, symbol, trade_id, e)
            with _float_bulk_jobs_lock:
                job = _float_bulk_jobs[job_id]
                job["trades_updated"] += len(trade_ids)
                job["updated"].append({"symbol": symbol, "float_shares": shares, "float_tag": float_tag, "trade_ids": trade_ids})
        except Exception as e:
            log.warning("Bulk float job %s: lookup for %s failed: %s", job_id, symbol, e)
            with _float_bulk_jobs_lock:
                _float_bulk_jobs[job_id]["errors"].append({"symbol": symbol, "error": str(e)})

        with _float_bulk_jobs_lock:
            _float_bulk_jobs[job_id]["current_index"] = i + 1  # this symbol is now done too

    with _float_bulk_jobs_lock:
        if _float_bulk_jobs[job_id]["status"] == "running":
            _float_bulk_jobs[job_id]["status"] = "done"

@app.route("/fetch-float/bulk/start", methods=["POST", "OPTIONS"])
def fetch_float_bulk_start():
    """Kicks off a background job that finds every trade of the calling
    user's with no float yet (see float_shares_store.list_trades_missing_float),
    groups them by symbol, and works through the symbol list one at a time
    (see _run_float_bulk_job), same start->poll->cancel shape as
    /backtest/start below. Returns immediately with a job_id -- poll
    /fetch-float/bulk/status/<job_id> for progress."""
    if request.method == "OPTIONS":
        return "", 204

    user_id, err = _require_user()
    if err:
        return err

    try:
        missing = float_shares_store.list_trades_missing_float(user_id)
    except Exception as e:
        log.warning("list_trades_missing_float failed for user %s: %s", user_id, e)
        return jsonify({"error": f"Couldn't look up trades: {e}"}), 502

    symbol_to_trade_ids: dict = {}
    for row in missing:
        symbol = (row.get("symbol") or "").strip().upper()
        trade_id = row.get("id")
        if not symbol or not trade_id:
            continue
        symbol_to_trade_ids.setdefault(symbol, []).append(trade_id)

    job_id = uuid.uuid4().hex[:12]
    with _float_bulk_jobs_lock:
        _float_bulk_jobs[job_id] = {
            "status": "running" if symbol_to_trade_ids else "done",
            "total_symbols": len(symbol_to_trade_ids),
            "current_index": 0,
            "current_symbol": None,
            "total_trades": sum(len(v) for v in symbol_to_trade_ids.values()),
            "trades_updated": 0,
            "updated": [],
            "errors": [],
            "cancel_requested": False,
            "user_id": user_id,  # stripped before responding, same as _backtest_jobs
        }

    if symbol_to_trade_ids:
        t = threading.Thread(target=_run_float_bulk_job, args=(job_id, user_id, symbol_to_trade_ids), daemon=True)
        t.start()
    return jsonify({"job_id": job_id, "total_symbols": len(symbol_to_trade_ids), "total_trades": sum(len(v) for v in symbol_to_trade_ids.values())})

@app.route("/fetch-float/bulk/status/<job_id>", methods=["GET"])
def fetch_float_bulk_status(job_id):
    user_id, err = _require_user()
    if err:
        return err
    with _float_bulk_jobs_lock:
        job = _float_bulk_jobs.get(job_id)
    if job is None or job.get("user_id") != user_id:
        return jsonify({"status": "unknown"}), 404
    return jsonify({k: v for k, v in job.items() if k != "user_id"})

@app.route("/fetch-float/bulk/cancel/<job_id>", methods=["POST", "OPTIONS"])
def fetch_float_bulk_cancel(job_id):
    # Cooperative cancel, same as /backtest/cancel: flips a flag
    # _run_float_bulk_job checks between symbols. The symbol currently
    # in flight finishes (it's one Polygon call plus a handful of small
    # writes, never long), everything after it stops. Whatever ran
    # already stays saved on those trades.
    if request.method == "OPTIONS":
        return "", 204
    user_id, err = _require_user()
    if err:
        return err
    with _float_bulk_jobs_lock:
        job = _float_bulk_jobs.get(job_id)
        if job is None or job.get("user_id") != user_id:
            return jsonify({"error": "unknown job"}), 404
        if job["status"] != "running":
            return jsonify({"status": job["status"]})
        job["cancel_requested"] = True
    return jsonify({"status": "cancelling"})

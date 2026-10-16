"""
setup_routes.py
POST /classify-setups -- tags trades with the trader's own setups
(red_to_green / green_over_green_wick / ema9_dip_buy / chase / other) by reading the
real 1-minute bars. Used by the Journal's "Tag my setups" backfill; new imports
get the same tag automatically through compute_trade_levels (market_data.py).

Body: {"trades": [{"id", "symbol", "trade_date": "YYYY-MM-DD",
                   "entry_time": "HH:MM:SS", "exit_time", "entry_price", "exit_price",
                   "shares", "pnl_after_comm", "side"}],
       "day_trades": [same fields, every trade that day -- for revenge / overtrading / repeat symbol],
       "median_shares": number (your typical size -- for "oversized")}
Returns {"results": {id: {"setup_type", "matches", "tags"}}, "errors": {id: message}}.
"tags" are the behavior tags from trade_tags.py (session, scaling, revenge, VWAP/EMA9, ...).

The browser sends one symbol+day per request (bars are fetched once per
symbol+day and go through the same cache/limiter as /full-day-bars), so a
request never queues behind more than one Polygon fetch.
"""

from datetime import datetime
from concurrent.futures import TimeoutError as FutureTimeoutError

import requests

from flask import request, jsonify

from config import app, log, ET, _require_user, _request_pool, REQUEST_HARD_TIMEOUT_S
from market_data import get_full_day_bars
import setups
import tag_catalog as _tag_catalog
import trade_tags

MAX_TRADES_PER_REQUEST = 60
MAX_DAY_TRADES = 300


def _fetch_fills(user_id, ids: list) -> dict:
    """{trade_id: fills[]} from trade_details (the index rows don't carry fills). Best effort:
    without them only the scaled-in/out and added-to-loser tags are skipped."""
    try:
        from publish import SUPABASE_URL, _supabase_headers
        resp = requests.get(f"{SUPABASE_URL}/rest/v1/trade_details", headers=_supabase_headers(), timeout=20, params={
            "user_id": f"eq.{user_id}", "trade_id": "in.(" + ",".join(ids) + ")", "select": "trade_id,fills"})
        resp.raise_for_status()
        return {r["trade_id"]: (r.get("fills") or []) for r in resp.json()}
    except Exception as e:
        log.warning("classify-setups: couldn't read fills: %s", e)
        return {}


def _classify_group(symbol: str, trade_date: str, trades: list, day_trades: list, median_shares, fills_by_id: dict) -> dict:
    bars = get_full_day_bars(symbol, trade_date)
    results, errors = {}, {}
    for t in trades:
        tid = str(t.get("id"))
        try:
            entry_dt = datetime.fromisoformat(f"{trade_date}T{t['entry_time']}").replace(tzinfo=ET)
            price = float(t["entry_price"])
            side = "long"  # the trader only buys; any short row is tagged as the long plan would be
            m = setups.matches(bars, entry_dt, price, side)
            tags = trade_tags.compute_tags(
                {**t, "trade_date": trade_date, "fills": fills_by_id.get(tid, [])}, day_trades, bars, median_shares)
            results[tid] = {"setup_type": m[0] if m else setups.OTHER, "matches": m, "tags": tags}
        except Exception as e:  # one bad row must not sink the rest
            errors[tid] = f"{type(e).__name__}: {e}"
    return {"results": results, "errors": errors}


@app.route("/classify-setups", methods=["POST", "OPTIONS"])
def classify_setups():
    if request.method == "OPTIONS":
        return "", 204
    user_id, err = _require_user()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    trades = body.get("trades") or []
    if not isinstance(trades, list) or not trades:
        return jsonify({"error": "trades (non-empty list) is required"}), 400
    if len(trades) > MAX_TRADES_PER_REQUEST:
        return jsonify({"error": f"at most {MAX_TRADES_PER_REQUEST} trades per request"}), 400

    day_trades = body.get("day_trades") or []
    if not isinstance(day_trades, list) or len(day_trades) > MAX_DAY_TRADES:
        day_trades = []
    try:
        median_shares = float(body.get("median_shares")) if body.get("median_shares") else None
    except (TypeError, ValueError):
        median_shares = None
    fills_by_id = _fetch_fills(user_id, [str(t.get("id")) for t in trades if t.get("id")])

    groups = {}
    for t in trades:
        key = ((t.get("symbol") or "").strip().upper(), t.get("trade_date") or "")
        groups.setdefault(key, []).append(t)

    results, errors = {}, {}
    for (symbol, trade_date), grp in groups.items():
        if not symbol or not trade_date:
            for t in grp:
                errors[str(t.get("id"))] = "symbol and trade_date are required"
            continue
        future = _request_pool.submit(_classify_group, symbol, trade_date, grp, day_trades, median_shares, fills_by_id)
        try:
            out = future.result(timeout=REQUEST_HARD_TIMEOUT_S)
            results.update(out["results"])
            errors.update(out["errors"])
        except FutureTimeoutError:
            msg = f"timed out fetching bars for {symbol} {trade_date}"
            log.error("classify-setups: %s", msg)
            for t in grp:
                errors[str(t.get("id"))] = msg
        except Exception as e:
            log.warning("classify-setups: %s %s failed: %s", symbol, trade_date, e)
            for t in grp:
                errors[str(t.get("id"))] = f"{type(e).__name__}: {e}"
    return jsonify({"results": results, "errors": errors})


@app.route("/tag-catalog", methods=["GET", "OPTIONS"])
def tag_catalog_route():
    """What every auto tag means (group, tone, plain-English rule with the live thresholds). Static,
    nothing user-specific, so no login is needed -- it feeds the site's Tag guide and tap-a-tag sheet."""
    if request.method == "OPTIONS":
        return "", 204
    resp = jsonify(_tag_catalog.build())
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp

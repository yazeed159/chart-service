"""
strategies_routes.py
Strategy CRUD (/strategies, /strategies/<id>) and turning a completed
backtest run into a saved strategy (/backtest/history/<job_id>/save-strategy
-- URL-grouped with the backtester, but its own logic is really about
building a strategy_store row, so it lives here). Split out of the old
chart_service.py -- see chart_service.py's module docstring for how these
files fit together.
"""

import uuid
from datetime import datetime

from flask import request, jsonify

import strategy_store
import backtest_storage
from config import app, _require_user, _JOB_ID_RE

# A run's stored `params` (backtest_start's raw request body) mixes
# together three different kinds of settings: the gap-scan rule that
# picked candidate symbols (top_n/min_price/max_price/min_dollar_volume/
# min_gap_pct), the entry/stop/exit rule itself (entry_mode, stop_mode,
# target_r, ...), and backtest-only account simulation settings that have
# no live-trading equivalent (starting_capital, position_size, ...).
# Splitting those apart is exactly what turns "a completed backtest run"
# into "a strategy live-service can start a run from".
_SYMBOL_RULE_KEYS = {"top_n", "min_price", "max_price", "min_dollar_volume", "min_gap_pct"}

_BACKTEST_ONLY_KEYS = {
    "start", "end", "label", "position_size", "position_sizing_mode",
    "position_size_pct", "risk_pct_of_capital", "starting_capital",
    "include_commissions",
}

@app.route("/strategies", methods=["GET", "POST", "OPTIONS"])
def strategies_list_create():
    if request.method == "OPTIONS":
        return "", 204
    user_id, err = _require_user()
    if err:
        return err

    if request.method == "GET":
        return jsonify(strategy_store.list_strategies(user_id))

    # POST: create a strategy directly (not from a backtest run) -- e.g.
    # a future "new strategy" flow on the live-trading page itself.
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    entry_mode = body.get("entry_mode") or ""
    if not name or not entry_mode:
        return jsonify({"error": "name and entry_mode are required"}), 400
    strategy_id = uuid.uuid4().hex[:12]
    row = strategy_store.save_strategy(
        strategy_id, user_id, datetime.now().isoformat(timespec="seconds"),
        name, entry_mode,
        params=body.get("params") or {},
        symbol_rule=body.get("symbol_rule") or {"mode": "manual", "symbols": []},
        source_backtest_run_id=None,
        source_summary_stats={},
    )
    return jsonify(row)

@app.route("/strategies/<strategy_id>", methods=["GET", "PUT", "DELETE", "OPTIONS"])
def strategies_get_delete(strategy_id):
    if request.method == "OPTIONS":
        return "", 204
    user_id, err = _require_user()
    if err:
        return err
    if request.method == "DELETE":
        strategy_store.delete_strategy(strategy_id, user_id)
        return jsonify({"deleted": strategy_id})
    if request.method == "PUT":
        # Edits an existing strategy's params/entry_mode/symbol_rule/name
        # in place -- e.g. Live Trading's param editor saving changes
        # "forever". Same row both the Backtester and Live Trading read,
        # so this is also how a live-tuned param takes effect back in
        # the Backtester next time this strategy is loaded there.
        body = request.get_json(force=True, silent=True) or {}
        row = strategy_store.update_strategy(
            strategy_id, user_id,
            name=body.get("name"),
            entry_mode=body.get("entry_mode"),
            params=body.get("params"),
            symbol_rule=body.get("symbol_rule"),
        )
        if row is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(row)
    row = strategy_store.get_strategy(strategy_id, user_id)
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(row)

@app.route("/backtest/history/<job_id>/save-strategy", methods=["POST", "OPTIONS"])
def backtest_save_strategy(job_id):
    """Turns a completed backtest run into a reusable strategy -- pulls
    entry_mode/params/symbol_rule out of the run's stored params (see the
    key-splitting comment above), and copies its summary_stats over as a
    point-in-time reference so a strategy picker can show how it
    backtested without a join back to backtest_runs (which may later get
    deleted)."""
    if request.method == "OPTIONS":
        return "", 204
    user_id, err = _require_user()
    if err:
        return err
    if not _JOB_ID_RE.match(job_id or ""):
        return jsonify({"error": "unknown run"}), 404

    run = backtest_storage.load_backtest_run_light(job_id, user_id)
    if run is None:
        return jsonify({"error": "unknown run"}), 404

    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    run_params = dict(run.get("params") or {})
    entry_mode = run_params.pop("entry_mode", None)
    if not entry_mode:
        return jsonify({"error": "this run has no entry_mode -- can't save as a strategy"}), 400

    symbol_rule = {
        "mode": "top_gappers",
        "top_n": run_params.pop("top_n", 5),
        "min_price": run_params.pop("min_price", 1.0),
        "max_price": run_params.pop("max_price", 50.0),
        "min_dollar_volume": run_params.pop("min_dollar_volume", 5_000_000),
        "min_gap_pct": run_params.pop("min_gap_pct", 5.0),
    }
    # Allow the request to override the rule (e.g. the person picked
    # specific symbols on the save dialog instead of keeping the scan
    # rule) -- otherwise keep what the run actually used.
    symbol_rule.update(body.get("symbol_rule") or {})

    strategy_params = {k: v for k, v in run_params.items() if k not in _BACKTEST_ONLY_KEYS}

    strategy_id = uuid.uuid4().hex[:12]
    row = strategy_store.save_strategy(
        strategy_id, user_id, datetime.now().isoformat(timespec="seconds"),
        name, entry_mode, strategy_params, symbol_rule,
        source_backtest_run_id=job_id,
        source_summary_stats=run.get("summary_stats") or {},
    )
    return jsonify(row)

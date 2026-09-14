"""
backtest_routes.py
The ORB / gap-gainer backtester's /backtest/* routes (config defaults,
start/status/cancel, and history/report/enrich/delete) -- powers the
"Backtester" tab in the dashboard (backtester.html). Split out of the old
chart_service.py -- see chart_service.py's module docstring for how these
files fit together.
"""

import uuid
import threading
from datetime import date, datetime

from flask import request, jsonify

from engine import BacktestConfig, run_backtest, compute_stats, BacktestCancelled
from orb_strategy import DEFAULT_PARAMS as ORB_DEFAULT_PARAMS
import backtest_storage
from config import app, log, _require_user, _JOB_ID_RE

_backtest_jobs = {}

_backtest_jobs_lock = threading.Lock()

def _backtest_report_load(job_id, user_id):
    # job ids are always uuid4().hex[:12] -- reject anything else before
    # it goes anywhere near a query, same guard the old file path had.
    if not _JOB_ID_RE.match(job_id or ""):
        return None
    return backtest_storage.load_backtest_report(job_id, user_id)

def _backtest_report_delete(job_id, user_id):
    if not _JOB_ID_RE.match(job_id or ""):
        return
    backtest_storage.delete_backtest_run(job_id, user_id)

def _run_backtest_job(job_id: str, user_id: str, cfg: BacktestConfig, meta: dict):
    def progress(i, total, d, trades_so_far=None):
        with _backtest_jobs_lock:
            job = _backtest_jobs.get(job_id)
            if job is None:
                return
            job["current"] = i + 1
            job["total"] = total
            job["day"] = d.isoformat()
            # Keep partial trades + stats up to date after every day so a
            # mid-run poll (or a cancel) still has something real to show,
            # instead of only ever populating these once the whole run ends.
            if trades_so_far is not None:
                job["trades"] = trades_so_far
                job["stats"] = compute_stats(trades_so_far, starting_capital=cfg.starting_capital)

    def cancel_check():
        # Polled before every symbol (see engine.py), not just once per
        # day, so hitting Cancel takes effect within a couple seconds
        # instead of waiting for the whole in-flight day to finish.
        with _backtest_jobs_lock:
            job = _backtest_jobs.get(job_id)
            return bool(job and job.get("cancel_requested"))

    try:
        trades = run_backtest(cfg, progress_cb=progress, cancel_check=cancel_check)
        stats = compute_stats(trades, starting_capital=cfg.starting_capital)

        with _backtest_jobs_lock:
            _backtest_jobs[job_id]["status"] = "done"
            _backtest_jobs[job_id]["stats"] = stats
            _backtest_jobs[job_id]["trades"] = trades

        created_at = datetime.now().isoformat(timespec="seconds")
        label = meta.get("label") or "(untitled run)"
        summary_stats = {
            "num_trades": stats["num_trades"],
            "win_rate": stats["win_rate"],
            "profit_factor": stats["profit_factor"],
            "net_pnl_dollars": stats["net_pnl_dollars"],
            "avg_r": stats["avg_r"],
            "max_drawdown_dollars": stats["max_drawdown_dollars"],
            "total_commissions_dollars": stats.get("total_commissions_dollars", 0.0),
        }
        # One upsert covers both the Past Runs list (summary_stats) and the
        # full report (every trade, full stats incl. equity curve) -- see
        # GET /backtest/history/<job_id>/report for the latter.
        try:
            backtest_storage.save_backtest_run(
                job_id, user_id, created_at, label, meta, summary_stats,
                report={
                    "id": job_id, "created_at": created_at, "label": label,
                    "params": meta, "stats": stats, "trades": trades,
                },
            )
        except Exception:
            # Don't let a Supabase hiccup lose the run entirely -- it's
            # still sitting in _backtest_jobs and pollable/downloadable
            # until this process restarts, just won't show up in Past Runs.
            log.exception("Backtest job %s: failed to persist to Supabase", job_id)
    except BacktestCancelled:
        log.info("Backtest job %s cancelled by user", job_id)
        with _backtest_jobs_lock:
            job = _backtest_jobs[job_id]
            job["status"] = "cancelled"
            # job["trades"]/job["stats"] already hold whatever was finished
            # as of the last completed day -- leave them as-is so the
            # person can still see/download the partial report.
    except Exception as e:
        log.exception("Backtest job %s failed", job_id)
        with _backtest_jobs_lock:
            _backtest_jobs[job_id]["status"] = "error"
            _backtest_jobs[job_id]["error"] = str(e)

def _num(body, name, default, cast=float):
    v = body.get(name, default)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default

@app.route("/backtest/defaults", methods=["GET"])
def backtest_defaults():
    """So backtester.js doesn't have to hardcode a second copy of
    orb_strategy.py's defaults -- it fetches this once to prefill the form."""
    return jsonify(dict(ORB_DEFAULT_PARAMS))

@app.route("/backtest/start", methods=["POST", "OPTIONS"])
def backtest_start():
    if request.method == "OPTIONS":
        return "", 204

    user_id, err = _require_user()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}

    try:
        start = datetime.strptime(body.get("start", ""), "%Y-%m-%d").date()
        end = datetime.strptime(body.get("end", ""), "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "start/end must be YYYY-MM-DD"}), 400
    if end < start:
        return jsonify({"error": "end date is before start date"}), 400
    if end > date.today():
        return jsonify({"error": "end date can't be in the future"}), 400

    defaults = ORB_DEFAULT_PARAMS
    strategy_params = {
        "entry_mode": body.get("entry_mode", defaults["entry_mode"]),
        "orb_minutes": _num(body, "orb_minutes", defaults["orb_minutes"], int),
        "entry_after_orb": bool(body.get("entry_after_orb", defaults["entry_after_orb"])),
        "donchian_lookback": _num(body, "donchian_lookback", defaults["donchian_lookback"], int),
        "stop_mode": body.get("stop_mode", defaults["stop_mode"]),
        "fixed_stop_cents": _num(body, "fixed_stop_cents", defaults["fixed_stop_cents"]),
        "fixed_stop_pct": _num(body, "fixed_stop_pct", defaults["fixed_stop_pct"]),
        "atr_period": _num(body, "atr_period", defaults["atr_period"], int),
        "atr_mult": _num(body, "atr_mult", defaults["atr_mult"]),
        "breakeven_after_cents": _num(body, "breakeven_after_cents", None) or None,
        "ema_period": _num(body, "ema_period", defaults.get("ema_period", 9), int),
        "macd_fast": _num(body, "macd_fast", defaults.get("macd_fast", 12), int),
        "macd_slow": _num(body, "macd_slow", defaults.get("macd_slow", 26), int),
        "macd_signal": _num(body, "macd_signal", defaults.get("macd_signal", 9), int),
        "rsi_period": _num(body, "rsi_period", defaults.get("rsi_period", 14), int),
        "rsi_oversold": _num(body, "rsi_oversold", defaults.get("rsi_oversold", 30.0)),
        "target_r": _num(body, "target_r", defaults["target_r"]) or None,
        "time_stop_minutes": _num(body, "time_stop_minutes", None) or None,
        "time_stop_min_gain_cents": _num(body, "time_stop_min_gain_cents", 0.0),
        "giveback_cents": _num(body, "giveback_cents", None) or None,
        "giveback_pct": _num(body, "giveback_pct", None) or None,
        "giveback_arm_cents": _num(body, "giveback_arm_cents", 0.0),
        "stall_exit": bool(body.get("stall_exit", False)),
        "ema_close_exit": bool(body.get("ema_close_exit", defaults.get("ema_close_exit", False))),
        "require_macd_confirmation": bool(body.get("require_macd_confirmation", defaults.get("require_macd_confirmation", False))),
        # Re-entry: on by default (see orb_strategy.py's DEFAULT_PARAMS) --
        # a symbol/day can produce more than one trade unless the request
        # explicitly turns it off.
        "allow_reentry": bool(body.get("allow_reentry", defaults.get("allow_reentry", True))),
        "max_trades_per_day": _num(body, "max_trades_per_day", defaults.get("max_trades_per_day", 3), int),
        "reentry_cooldown_minutes": _num(body, "reentry_cooldown_minutes", defaults.get("reentry_cooldown_minutes", 0.0)),
        "slippage_bps": _num(body, "slippage_bps", defaults.get("slippage_bps", 5.0)),
        # Position building -- "buy some, add on strength" (see
        # orb_strategy.py's POSITION BUILDING section). Off unless the
        # request explicitly turns it on.
        "scale_in_enabled": bool(body.get("scale_in_enabled", False)),
        "scale_in_initial_size_pct": _num(body, "scale_in_initial_size_pct", defaults.get("scale_in_initial_size_pct", 50.0)),
        "scale_in_add_size_pct": _num(body, "scale_in_add_size_pct", defaults.get("scale_in_add_size_pct", 25.0)),
        "scale_in_max_adds": _num(body, "scale_in_max_adds", defaults.get("scale_in_max_adds", 2), int),
        "scale_in_hold_bars": _num(body, "scale_in_hold_bars", defaults.get("scale_in_hold_bars", 2), int),
        "scale_in_min_gain_cents": _num(body, "scale_in_min_gain_cents", defaults.get("scale_in_min_gain_cents", 10.0)),
        # Tightening trail-protect stop -- ratchets up to lock in a growing
        # fraction of the peak gain as the trade's peak R climbs. Off
        # unless the request explicitly turns it on. Ladder is sent (and
        # returned by /backtest/defaults) as a flat list of [r, protect_frac]
        # pairs -- JSON has no tuples, but a 2-element array round-trips
        # through jsonify() the same way orb_strategy.py's tuple defaults do.
        "trail_protect_enabled": bool(body.get("trail_protect_enabled", False)),
        "trail_protect_ladder": [
            (float(rung[0]), float(rung[1]))
            for rung in (body.get("trail_protect_ladder") or [])
            if isinstance(rung, (list, tuple)) and len(rung) == 2
        ] or list(defaults.get("trail_protect_ladder", [])),
        "flatten_time": body.get("flatten_time", defaults["flatten_time"]),
        # Was hardcoded to "09:30" (regular-hours open) regardless of what
        # the form/AI-config panel sent, which silently threw away any
        # "start scanning at <time>" instruction (e.g. a pre-market ORB
        # session) -- fetch_minute_bars already returns pre/post-market
        # bars, orb_strategy already accepts session_open, this just wires
        # the request body's value through instead of ignoring it.
        "session_open": body.get("session_start", defaults.get("session_open", "09:30")),
    }

    cfg = BacktestConfig(
        start_date=start, end_date=end,
        top_n=int(_num(body, "top_n", 5, int)),
        min_price=_num(body, "min_price", 1.0), max_price=_num(body, "max_price", 50.0),
        min_dollar_volume=_num(body, "min_dollar_volume", 5_000_000),
        min_gap_pct=_num(body, "min_gap_pct", 5.0),
        position_size_dollars=_num(body, "position_size", 2000.0),
        strategy_params=strategy_params,
        include_commissions=bool(body.get("include_commissions", True)),
        starting_capital=_num(body, "starting_capital", 25_000.0),
        position_sizing_mode=body.get("position_sizing_mode", "fixed_dollars"),
        position_size_pct=_num(body, "position_size_pct", 10.0),
        risk_pct_of_capital=_num(body, "risk_pct_of_capital", 1.0),
    )

    job_id = uuid.uuid4().hex[:12]
    with _backtest_jobs_lock:
        _backtest_jobs[job_id] = {
            "status": "running", "current": 0, "total": 0, "day": None,
            "trades": [], "stats": None, "cancel_requested": False,
            "user_id": user_id,  # not serialized out -- see backtest_status, which strips it before responding
        }

    t = threading.Thread(target=_run_backtest_job, args=(job_id, user_id, cfg, body), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})

@app.route("/backtest/status/<job_id>", methods=["GET"])
def backtest_status(job_id):
    user_id, err = _require_user()
    if err:
        return err
    with _backtest_jobs_lock:
        job = _backtest_jobs.get(job_id)
    # Someone else's job_id (or an unknown one) looks identical to a
    # missing job -- don't distinguish "exists but isn't yours" from
    # "never existed".
    if job is None or job.get("user_id") != user_id:
        return jsonify({"status": "unknown"}), 404
    return jsonify({k: v for k, v in job.items() if k != "user_id"})

@app.route("/backtest/cancel/<job_id>", methods=["POST", "OPTIONS"])
def backtest_cancel(job_id):
    # Cooperative cancel: flips a flag that _run_backtest_job's progress()
    # callback checks once it finishes the day currently in flight (that
    # in-flight day can't be interrupted mid-fetch, but everything after
    # it stops). Whatever trades/stats had already accumulated stay on the
    # job as a partial report -- see backtest_status.
    if request.method == "OPTIONS":
        return "", 204
    user_id, err = _require_user()
    if err:
        return err
    with _backtest_jobs_lock:
        job = _backtest_jobs.get(job_id)
        if job is None or job.get("user_id") != user_id:
            return jsonify({"error": "unknown job"}), 404
        if job["status"] != "running":
            return jsonify({"status": job["status"]})
        job["cancel_requested"] = True
    return jsonify({"status": "cancelling"})

@app.route("/backtest/history", methods=["GET"])
def backtest_history():
    user_id, err = _require_user()
    if err:
        return err
    return jsonify(backtest_storage.load_backtest_history(user_id))

@app.route("/backtest/history/<job_id>/report", methods=["GET"])
def backtest_history_report(job_id):
    # Full report for a past run -- every trade + full stats (equity curve
    # included) -- saved once at run-completion time (see
    # _run_backtest_job) and readable here at any point after, independent
    # of the in-memory _backtest_jobs dict, which is lost on a server
    # restart. This is what lets a Past Runs card reopen the actual report
    # instead of only re-running the same params. Scoped to the caller's
    # own runs -- see _backtest_report_load / backtest_storage.load_backtest_report.
    user_id, err = _require_user()
    if err:
        return err
    report = _backtest_report_load(job_id, user_id)
    if report is None:
        return jsonify({"error": "no saved report for this run (may predate this feature, may not be yours, or the run didn't finish)"}), 404
    # Once a run's charts have been generated, each trade's `bars` is a
    # full session's worth of 1-min OHLCV -- easily the overwhelming
    # majority of this payload's weight for a run of any real size, and
    # the report view only ever needs it lazily, one trade at a time, when
    # something is actually clicked (View Chart, a Best/Worst stat, an
    # Overview breakdown row). Strip it out of the default response --
    # replaced with a cheap has_bars flag so the UI still knows whether a
    # chart is available -- and serve it through the dedicated per-trade
    # endpoint below instead. ?full=1 restores the old all-at-once shape,
    # for anything that genuinely still needs every trade's bars at once.
    if request.args.get("full") != "1":
        trades = report.get("trades") or []
        report = dict(report)
        stripped = []
        for t in trades:
            t2 = {k: v for k, v in t.items() if k != "bars"}
            t2["has_bars"] = bool(t.get("bars"))
            stripped.append(t2)
        report["trades"] = stripped
    return jsonify(report)

@app.route("/backtest/history/<job_id>/report/trade-bars/<int:idx>", methods=["GET"])
def backtest_history_trade_bars(job_id, idx):
    """Lazily fetches one trade's bars by its index into the saved
    report's `trades` array (stable regardless of how the report page has
    the table currently sorted -- it's an index into the stored order,
    not whatever order is on screen). Backs the report page's on-demand
    chart views now that GET /report strips bars out by default (see
    above)."""
    user_id, err = _require_user()
    if err:
        return err
    report = _backtest_report_load(job_id, user_id)
    if report is None:
        return jsonify({"error": "no saved report for this run (may predate this feature, may not be yours, or the run didn't finish)"}), 404
    trades = report.get("trades") or []
    if idx < 0 or idx >= len(trades):
        return jsonify({"error": "trade index out of range"}), 404
    return jsonify({"bars": trades[idx].get("bars") or []})

@app.route("/backtest/history/<job_id>/enrich", methods=["POST", "OPTIONS"])
def backtest_history_enrich(job_id):
    """Callback target for the n8n trade-journal workflow's per-trade
    chart+vision-LLM pass on a backtest's trades (see sendToJournal() in
    backtester.js). Deliberately separate from data/trades.json and the
    shared Google Sheet real fills write to -- this only ever touches this
    ONE run's own backtest_reports/<job_id>.json, so a backtest never
    shows up in, or skews the stats of, the real trading dashboard. Each
    run stays its own self-contained mini report.

    Body: {"trades": [{"date", "symbol", "entry_time", <any of: verdict,
    bars, indicators, chart_image (a data: URL or http(s) URL, kept only
    as a fallback for consumers that can't render an interactive chart),
    lessons, better_entry_price, better_entry_reason, better_exit_price,
    better_exit_reason>, ...}, ...]}. `bars`/`indicators` are the same
    per-minute series /generate-chart already computes (see
    serialize_bars/compute_indicators above) -- passing them through lets
    the report page draw its own interactive candlestick chart with
    entry/exit markers client-side, the same way trade.js does for real
    journal trades, instead of only having a flat PNG to show. Each
    incoming trade is matched to one already in the saved report by
    (date, symbol, entry_time) and only the enrichment fields present are
    merged onto it -- fields already on the trade (P&L, commission, etc.)
    are left untouched. Unmatched incoming trades don't fail the whole
    batch, just get reported back under `unmatched` so a partial/retried
    n8n run doesn't lose whatever *did* match.
    """
    if request.method == "OPTIONS":
        return "", 204

    # Deliberately unauthenticated (unlike the other /backtest/history/*
    # routes): the caller here is n8n's server-to-server workflow, not the
    # logged-in person's browser, so there's no Supabase access token to
    # check -- see backtest_storage.save_backtest_report's docstring.
    if not _JOB_ID_RE.match(job_id or ""):
        return jsonify({"error": "no saved report for this run"}), 404
    report = backtest_storage.load_backtest_report_unscoped(job_id)
    if report is None:
        return jsonify({"error": "no saved report for this run"}), 404

    body = request.get_json(force=True, silent=True) or {}
    incoming = body.get("trades") or []
    ENRICH_FIELDS = (
        "verdict", "bars", "indicators", "chart_image", "lessons",
        "better_entry_price", "better_entry_reason",
        "better_exit_price", "better_exit_reason",
    )

    def key_of(t):
        return (t.get("date"), t.get("symbol"), t.get("entry_time"))

    by_key = {key_of(t): t for t in incoming}
    matched_keys = set()
    matched_but_empty = []
    for t in report.get("trades", []):
        src = by_key.get(key_of(t))
        if not src:
            continue
        for field in ENRICH_FIELDS:
            if field in src:
                t[field] = src[field]
        if "lessons" in src:
            # This callback's lessons come straight from n8n's own
            # Gemini call (backtester.js's "Send to Journal"), not
            # through daily_sync.py's normal per-fill pipeline, so they
            # never pass through _normalize_lessons there. Without this,
            # a double-encoded lesson (a JSON string instead of a real
            # object -- see _normalize_lessons's docstring) lands in
            # this run's saved report as-is and every consumer
            # (trade.js/quiz.js/rewind.js/share-export.js) prints it as
            # raw JSON text instead of the fields inside it.
            from daily_sync import _normalize_lessons
            t["lessons"] = _normalize_lessons(src["lessons"])
        matched_keys.add(key_of(t))
        if not (isinstance(t.get("bars"), list) and t["bars"]):
            matched_but_empty.append(key_of(t))

    unmatched_incoming = [k for k in by_key if k not in matched_keys]
    log.info(
        "Enrich %s: %d/%d incoming trades matched, %d matched-but-no-bars",
        job_id, len(matched_keys), len(incoming), len(matched_but_empty),
    )
    if unmatched_incoming:
        log.warning("Enrich %s: unmatched incoming keys (date, symbol, entry_time): %s", job_id, unmatched_incoming)
        log.warning("Enrich %s: keys already on the saved report: %s", job_id, [key_of(t) for t in report.get("trades", [])])
    if matched_but_empty:
        log.warning("Enrich %s: matched but bars came through empty/missing for: %s", job_id, matched_but_empty)

    backtest_storage.save_backtest_report(job_id, report)
    return jsonify({"matched": len(matched_keys), "unmatched": len(by_key) - len(matched_keys), "matched_but_empty": len(matched_but_empty)})

@app.route("/backtest/history/<job_id>", methods=["DELETE", "OPTIONS"])
def backtest_history_delete(job_id):
    if request.method == "OPTIONS":
        return "", 204
    user_id, err = _require_user()
    if err:
        return err
    # One row covers both the history-list entry and the full report now,
    # so deleting it removes both in a single call (used to be a
    # load-all/filter/save-all on the history file plus a separate report
    # file delete). Scoped to the caller's own runs -- someone else's
    # job_id matches zero rows and is a silent no-op, not an error.
    _backtest_report_delete(job_id, user_id)
    with _backtest_jobs_lock:
        job = _backtest_jobs.get(job_id)
        if job and job.get("user_id") == user_id:
            _backtest_jobs.pop(job_id, None)
    return jsonify({"deleted": job_id})

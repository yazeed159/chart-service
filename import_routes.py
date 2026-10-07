"""
import_routes.py
Flask Blueprint replacing n8n's "Import Historical Trades (Form)" trigger
through to publish -- the full CSV-import branch, not just the FIFO-match
step. In the original n8n graph, a CSV upload shares almost its entire
downstream path with the daily Flex sync: Extract & Match Trades1 ->
Generate Chart -> Vision LLM verdict -> Generate Final Chart -> Prepare
Trade Payloads -> Supabase publish. The only thing it skips is the
Google Sheets step ("Daily Run? (Skip Sheet Log for CSV Import)"). This
file used to stop right after the FIFO match and hand back bare trades
for review -- that was a real gap, not a deliberate simplification: a
CSV upload never actually reached the journal. Fixed here by reusing
daily_sync.process_trade() (chart -> verdict -> final chart) and
publish.publish_trades() (Supabase writes), same as the daily pipeline.

WHO the trades belong to: unlike the daily pipeline (which always knows
the user via the broker_accounts row it's processing), a CSV upload from
the browser doesn't carry a user id anywhere by default -- the site's
other pages rely on Supabase Row Level Security + auth.uid() instead of
sending one. Since publishing happens with the service-role key (which
bypasses RLS), this endpoint requires the caller's Supabase access token
in `Authorization: Bearer <token>` and resolves the user id from it via
supabase_auth.resolve_user_id() -- see that file's docstring. Requests
with no/invalid token are rejected with 401 before anything is parsed.

PROGRESS + CANCEL: charting + the Gemini verdict run one trade at a time,
paced CHART_PACING_SECONDS apart (shared Polygon rate limit), so a
larger import can take several minutes. The background thread writes
{status, total, completed, current} to the job file after every trade so
GET /import-trades/<job_id> can show real progress instead of a bare
spinner, and POST /import-trades   (optional form field overwrite=1 re-does trades already in the journal;
                       otherwise those are skipped and listed in "already_imported")/<job_id>/cancel sets an in-memory
threading.Event the loop checks before starting each new trade -- it
won't abort a chart/Gemini call already in flight, but it stops picking
up further trades and immediately publishes whatever was already
enriched, so cancelling doesn't throw away completed work. Cancellation
state lives in a module-level dict (_cancel_events), same "single
background worker process" assumption the rest of this service already
makes (daily_sync / backtest_import_routes) -- it doesn't survive a
process restart, but a job's on-disk status does.

Register from chart_service.py the same way as the other blueprints:

    from import_routes import bp as import_bp
    app.register_blueprint(import_bp)

POST /import-trades
  Headers: Authorization: Bearer <supabase access token>   (required)
  multipart/form-data, one file field named "trade_csv".

  -> 202 { "job_id": <str>, "trades": [...matched, not yet charted/
           graded/published], "skipped_rows": <int> }
  -> 400 / 401 / 422 -- see below

GET /import-trades/<job_id>
  -> 200 { "status": "processing", "phase": "charting" | "publishing",
           "total": <int>, "completed": <int>,
           "current": {"symbol", "trade_date"} | null,
           "trade_states": [{"id", "symbol", "trade_date", "state", "note"}] }
           (state: queued | charting | charted | chart_failed | publishing |
            saved | published | skipped | failed -- updated live)
  -> 200 { "status": "done" | "cancelled", "total", "completed",
           "trades": [...enriched], "publish_results": [...] }
  -> 404 unknown job_id

POST /import-trades/<job_id>/cancel
  -> 200 { "status": "cancelling" } -- stops before the next trade;
           whatever's already enriched still gets published
  -> 404 unknown or already-finished job_id
"""

import os
import csv
import io
import json
import time
import logging
import threading
import uuid
from pathlib import Path

from flask import Blueprint, request, jsonify

from trade_matching import parse_csv_executions, fifo_match_and_merge, looks_like_csv_row
from supabase_auth import resolve_user_id

log = logging.getLogger("chart_service.import_routes")
bp = Blueprint("import_routes", __name__)

IMPORT_RUNS_DIR = Path(os.environ.get("IMPORT_RUNS_DIR", "import_runs"))

# Same reasoning as daily_sync.CHART_PACING_SECONDS -- shared Polygon rate limit.
CHART_PACING_SECONDS = 13

# job_id -> threading.Event, only while that job's background thread is
# alive. See module docstring's PROGRESS + CANCEL section.
_cancel_events: dict[str, threading.Event] = {}


def _job_path(job_id: str) -> Path:
    return IMPORT_RUNS_DIR / f"{job_id}.json"


def _write_job(job_id: str, data: dict):
    IMPORT_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _job_path(job_id).write_text(json.dumps(data, indent=2, default=str))


def _trade_id(t: dict) -> str:
    """Same id formula publish.prepare_trade_payload uses, so the browser can
    match live per-trade states to its table rows."""
    d = (t.get("Trade Date") or "").replace("-", "")
    e = (t.get("Entry Time") or "").replace(":", "")
    return f"{t.get('Symbol')}-{d}-{e}"


def _existing_trade_ids(user_id, ids: list[str]) -> set[str] | None:
    """Which of these trade ids are already fully imported -- i.e. have a
    trade_details row (the trades index row is written first, so a trade
    whose detail step failed has an index row but no detail, and gets
    retried). Returns None if the lookup failed
    (caller then just processes everything -- safe, since saving is an
    upsert)."""
    import requests
    from publish import SUPABASE_URL, _supabase_headers
    found: set[str] = set()
    uniq = list(dict.fromkeys(ids))
    try:
        for i in range(0, len(uniq), 80):
            chunk = uniq[i:i + 80]
            quoted = ",".join('"' + c.replace('"', "") + '"' for c in chunk)
            resp = requests.get(
                f"{SUPABASE_URL}/rest/v1/trade_details",
                params={"user_id": f"eq.{user_id}", "trade_id": f"in.({quoted})", "select": "trade_id"},
                headers=_supabase_headers(), timeout=20,
            )
            resp.raise_for_status()
            found.update(r["trade_id"] for r in resp.json())
        return found
    except Exception as e:
        log.warning("existing-trade lookup failed (will process all): %s", e)
        return None


def _run_import_job(job_id: str, trades: list[dict], cancel_event: threading.Event, skip_ids: set | None = None):
    """Background thread: for each trade in turn (checking cancel_event
    before each) chart -> verdict -> final chart, then publish THAT trade
    immediately, so one failure never takes the others down and everything
    that succeeds is already saved. Never raises -- process_trade degrades
    gracefully per-trade, and publish_trades marks anything it can't publish
    as 'failed'/'skipped' rather than throwing."""
    from daily_sync import process_trade  # lazy import, same pattern as everywhere else in this service
    from publish import publish_trades

    total = len(trades)
    enriched = []
    cancelled = False

    # Live per-trade state, written into the job file after every change so
    # GET /import-trades/<job_id> can show each trade's status while it runs.
    # state: queued | charting | charted | chart_failed | publishing | saved |
    #        published | skipped | failed
    states = {}
    order = []
    for t in trades:
        tid = _trade_id(t)
        order.append(tid)
        states[tid] = {"id": tid, "symbol": t.get("Symbol"), "trade_date": t.get("Trade Date"), "state": "queued", "note": ""}

    def _flush(phase, completed, current=None, status="processing"):
        _write_job(job_id, {
            "status": status, "phase": phase, "total": total, "completed": completed,
            "current": current, "trade_states": [states[i] for i in order],
        })

    def _set(tid, state, note=""):
        if tid in states:
            states[tid]["state"] = state
            states[tid]["note"] = note

    publish_results = []
    done_count = 0
    skip_ids = skip_ids or set()
    did_work = False  # pace only after a trade that actually hit the data/AI APIs

    for i, trade in enumerate(trades):
        if cancel_event.is_set():
            cancelled = True
            log.info("import job %s: cancelled after %d/%d trades", job_id, i, total)
            break

        tid = _trade_id(trade)
        if tid in skip_ids:
            reason = "already in your journal -- skipped (tick 'overwrite' to redo it)"
            _set(tid, "skipped", reason)
            publish_results.append({"status": "skipped", "id": tid, "reason": reason})
            enriched.append(trade)
            done_count += 1
            _flush("charting", done_count, None)
            continue

        if did_work:
            time.sleep(CHART_PACING_SECONDS)
        did_work = True
        cur = {"symbol": trade.get("Symbol"), "trade_date": trade.get("Trade Date")}
        _set(tid, "charting", "building chart + AI verdict")
        _flush("charting", done_count, cur)
        try:
            result = process_trade(trade)
            if result.get("_final_bars") and result.get("_final_indicators"):
                _set(tid, "charted", "chart + verdict ready, publishing")
            else:
                _set(tid, "chart_failed", (result.get("_chart_error") or "no chart data returned (price bars unavailable)") + " -- will be skipped")
        except Exception as e:
            log.error("import job %s: process_trade crashed for %s %s: %s", job_id, trade.get("Symbol"), trade.get("Trade Date"), e)
            trade["_final_indicators"] = None
            trade["_final_bars"] = None
            trade["_final_image_base64"] = None
            trade["_chart_error"] = f"chart/verdict crashed: {type(e).__name__}: {e}"[:250]
            result = trade
            _set(tid, "chart_failed", result["_chart_error"])
        enriched.append(result)

        # Publish THIS trade right away (detail row + index row) so one bad
        # trade can't sink the rest, and everything that works is saved as we go.
        _flush("publishing", done_count, cur)

        def _on_event(trade_id, state, reason="", _cur=cur, _n=done_count):
            _set(trade_id, state, reason)
            _flush("publishing", _n, _cur)

        try:
            publish_results.extend(publish_trades([result], on_event=_on_event))
        except Exception as e:
            log.error("import job %s: publish crashed for %s: %s", job_id, tid, e)
            reason = f"publish crashed: {type(e).__name__}: {e}"[:250]
            _set(tid, "failed", reason)
            publish_results.append({"status": "failed", "id": tid, "reason": reason})
        done_count += 1
        _flush("charting", done_count, None)

    # Trades never reached because of a cancel
    if cancelled:
        for t in trades[len(enriched):]:
            _set(_trade_id(t), "skipped", "cancelled before this trade ran")

    _write_job(job_id, {
        "status": "cancelled" if cancelled else "done", "phase": "finished",
        "total": total, "completed": len(enriched),
        "trades": enriched, "publish_results": publish_results,
        "trade_states": [states[i] for i in order],
    })
    _cancel_events.pop(job_id, None)
    log.info("import job %s: %s, %d/%d trades processed", job_id, "cancelled" if cancelled else "done", len(enriched), total)


@bp.route("/import-trades", methods=["POST", "OPTIONS"])
def import_trades():
    if request.method == "OPTIONS":
        return "", 204

    user_id = resolve_user_id(request.headers.get("Authorization"))
    if not user_id:
        return jsonify({"error": "missing or invalid Authorization token -- please log in and try again"}), 401

    if "trade_csv" not in request.files:
        return jsonify({"error": "no file uploaded -- expected a multipart field named 'trade_csv'"}), 400

    file = request.files["trade_csv"]
    if not file.filename:
        return jsonify({"error": "empty file"}), 400

    try:
        raw = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"error": "couldn't read the file as UTF-8 text -- is it really a CSV?"}), 400

    reader = csv.DictReader(io.StringIO(raw))
    rows = list(reader)
    if not rows:
        return jsonify({"error": "CSV had no data rows"}), 400

    valid_rows = [r for r in rows if looks_like_csv_row(r)]
    skipped = len(rows) - len(valid_rows)
    if not valid_rows:
        return jsonify({
            "error": (
                "no row in this file had a recognizable symbol + date/time + price column. "
                "Headers found: " + ", ".join(rows[0].keys())
            )
        }), 422

    raw_executions = parse_csv_executions(valid_rows)
    ignored: list = []
    try:
        from publish import resolve_account_id  # lazy import, same pattern as elsewhere in this service
        target_account = resolve_account_id(user_id, (request.form.get("account_id") or "").strip() or None)
        closed_trades = fifo_match_and_merge(raw_executions, account={"user_id": user_id, "account_id": target_account}, skipped=ignored)
    except Exception as e:
        log.error("import-trades: FIFO matching failed: %s", e)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    log.info(
        "import-trades: user %s: %d rows -> %d closed trades (%d skipped, %d incomplete positions ignored)",
        user_id, len(rows), len(closed_trades), skipped, len(ignored),
    )
    for ig in ignored:
        log.info("import-trades: ignored %s %s: %s", ig.get("symbol"), ig.get("date"), ig.get("reason"))

    overwrite = (request.form.get("overwrite") or "").strip().lower() in ("1", "true", "on", "yes")
    skip_ids: set = set()
    lookup_failed = False
    if not overwrite:
        existing = _existing_trade_ids(user_id, [_trade_id(t) for t in closed_trades])
        if existing is None:
            lookup_failed = True
        else:
            skip_ids = existing
    log.info("import-trades: %d of %d trades already in journal (overwrite=%s)", len(skip_ids), len(closed_trades), overwrite)

    job_id = uuid.uuid4().hex
    cancel_event = threading.Event()
    _cancel_events[job_id] = cancel_event
    _write_job(job_id, {"status": "processing", "total": len(closed_trades), "completed": 0, "current": None})
    th = threading.Thread(target=_run_import_job, args=(job_id, closed_trades, cancel_event, skip_ids), daemon=True)
    th.start()

    return jsonify({"job_id": job_id, "trades": closed_trades, "skipped_rows": skipped,
                    "ignored_incomplete": ignored,
                    "already_imported": sorted(skip_ids), "existing_check_failed": lookup_failed}), 202


@bp.route("/import-trades/<job_id>", methods=["GET"])
def import_trades_status(job_id):
    path = _job_path(job_id)
    if not path.exists():
        return jsonify({"error": "unknown job_id"}), 404
    return jsonify(json.loads(path.read_text()))


@bp.route("/import-trades/<job_id>/cancel", methods=["POST"])
def import_trades_cancel(job_id):
    event = _cancel_events.get(job_id)
    if not event:
        return jsonify({"error": "unknown or already-finished job_id"}), 404
    event.set()

    path = _job_path(job_id)
    if path.exists():
        data = json.loads(path.read_text())
        if data.get("status") == "processing":
            data["status"] = "cancelling"
            _write_job(job_id, data)

    return jsonify({"status": "cancelling"}), 200

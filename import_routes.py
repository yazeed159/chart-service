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
spinner, and POST /import-trades/<job_id>/cancel sets an in-memory
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
  -> 200 { "status": "processing", "total": <int>, "completed": <int>,
           "current": {"symbol", "trade_date"} | null }
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


def _run_import_job(job_id: str, trades: list[dict], cancel_event: threading.Event):
    """Background thread: chart -> verdict -> final chart, one trade at a
    time (checking cancel_event before each), then publish whatever got
    enriched. Never raises -- process_trade already degrades gracefully
    per-trade, and publish_trades marks anything it can't publish as
    'failed'/'skipped' rather than throwing."""
    from daily_sync import process_trade  # lazy import, same pattern as everywhere else in this service
    from publish import publish_trades

    total = len(trades)
    enriched = []
    cancelled = False

    for i, trade in enumerate(trades):
        if cancel_event.is_set():
            cancelled = True
            log.info("import job %s: cancelled after %d/%d trades", job_id, i, total)
            break
        if i > 0:
            time.sleep(CHART_PACING_SECONDS)

        _write_job(job_id, {
            "status": "processing", "total": total, "completed": i,
            "current": {"symbol": trade.get("Symbol"), "trade_date": trade.get("Trade Date")},
        })
        try:
            enriched.append(process_trade(trade))
        except Exception as e:
            log.error("import job %s: process_trade crashed for %s %s: %s", job_id, trade.get("Symbol"), trade.get("Trade Date"), e)
            trade["_final_indicators"] = None
            trade["_final_bars"] = None
            trade["_final_image_base64"] = None
            enriched.append(trade)

    publish_results = []
    if enriched:
        try:
            publish_results = publish_trades(enriched)
        except Exception as e:
            log.error("import job %s: publish step failed entirely: %s", job_id, e)

    _write_job(job_id, {
        "status": "cancelled" if cancelled else "done",
        "total": total, "completed": len(enriched),
        "trades": enriched, "publish_results": publish_results,
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
    try:
        closed_trades = fifo_match_and_merge(raw_executions, account={"user_id": user_id})
    except Exception as e:
        log.error("import-trades: FIFO matching failed: %s", e)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    log.info(
        "import-trades: user %s: %d rows -> %d closed trades (%d skipped)",
        user_id, len(rows), len(closed_trades), skipped,
    )

    job_id = uuid.uuid4().hex
    cancel_event = threading.Event()
    _cancel_events[job_id] = cancel_event
    _write_job(job_id, {"status": "processing", "total": len(closed_trades), "completed": 0, "current": None})
    th = threading.Thread(target=_run_import_job, args=(job_id, closed_trades, cancel_event), daemon=True)
    th.start()

    return jsonify({"job_id": job_id, "trades": closed_trades, "skipped_rows": skipped}), 202


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

"""
app.py
Control API for the live trading engine -- start/stop strategy runs, poll
status, read recent signal/fill events. Frontend counterpart:
web-service/live-trading.html + live-trading.js.

Auth: same pattern chart-service's auth-gated routes use -- every request
needs 'Authorization: Bearer <supabase access token>', verified via
supabase_auth.resolve_user_id() (mounted read-only from the chart-service
repo -- see docker-compose.yml, no copy/drift). Since this is a
single-user bot, there's no per-user run isolation -- any request with a
valid token for YOUR Supabase project can control it. Don't expose
whatever tunnel URL you put in front of this anywhere public.

Threading note: ib_async needs a live asyncio event loop, and Flask's
dev/prod WSGI server is synchronous -- so the engine's event loop runs in
its own background thread, and every request hands its work to that loop
via asyncio.run_coroutine_threadsafe() and blocks for the result. Fine
for a low-request-rate control API like this; would need a real async
framework (FastAPI/Quart) if this ever needs to handle real concurrency.
"""

import asyncio
import logging
import os
import sys
import threading

from flask import Flask, jsonify, request, g
from flask_cors import CORS

sys.path.append(os.environ.get("ORB_STRATEGY_PATH", "/chart-service"))
import supabase_auth  # noqa: E402
import strategy_store  # noqa: E402 -- mounted read-only from chart-service, see docker-compose.yml

from engine import engine  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
from ib_async import util as _ib_util
_ib_util.logToConsole(logging.DEBUG)
log = logging.getLogger("live_app")

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=False)

_loop: asyncio.AbstractEventLoop | None = None
_loop_ready = threading.Event()


def _run_engine_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop_ready.set()
    _loop.run_forever()


threading.Thread(target=_run_engine_loop, daemon=True, name="engine-loop").start()
_loop_ready.wait(timeout=10)


def run_async(coro, timeout=20):
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=timeout)


def _authed_user_id() -> str | None:
    return supabase_auth.resolve_user_id(request.headers.get("Authorization"))


@app.before_request
def _check_auth():
    if request.method == "OPTIONS":
        # CORS preflight -- never carries the Authorization header, so
        # this must be allowed through unauthenticated or the browser
        # never even attempts the real request.
        return
    if request.path == "/health":
        return
    user_id = _authed_user_id()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    g.user_id = user_id


@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/api/live/strategies")
def strategies_list():
    return jsonify(strategy_store.list_strategies(g.user_id))


@app.route("/api/live/strategies/<strategy_id>")
def strategies_get(strategy_id):
    row = strategy_store.get_strategy(strategy_id, g.user_id)
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(row)


@app.route("/api/live/start", methods=["POST"])
def start():
    body = request.get_json(force=True) or {}
    mode = body.get("mode", "paper")
    strategy_id = body.get("strategy_id")
    # symbols/entry_mode/params can still be sent directly (back-compat,
    # and the escape hatch for a strategy whose symbol_rule isn't
    # "manual" -- see engine.start_run's docstring for why top_gappers
    # strategies still need symbols passed explicitly for now).
    symbols = body.get("symbols") or []
    entry_mode = body.get("entry_mode")
    params = body.get("params") or {}
    shares = int(body.get("shares_per_trade", 100))
    max_daily_loss = float(body.get("max_daily_loss_usd", 200))

    if mode not in ("paper", "live"):
        return jsonify({"error": "mode must be 'paper' or 'live'"}), 400
    if not strategy_id and not entry_mode:
        return jsonify({"error": "either strategy_id or entry_mode is required"}), 400

    try:
        run_id = run_async(engine.start_run(
            mode, symbols, entry_mode, params, shares, max_daily_loss,
            strategy_id=strategy_id, user_id=g.user_id,
        ))
    except Exception as e:
        log.exception("start_run failed")
        return jsonify({"error": str(e)}), 400
    return jsonify({"run_id": run_id})


@app.route("/api/live/<run_id>/stop", methods=["POST"])
def stop(run_id):
    body = request.get_json(silent=True) or {}
    flatten = bool(body.get("flatten", True))
    try:
        run_async(engine.stop_run(run_id, flatten=flatten))
    except KeyError:
        return jsonify({"error": "run not found"}), 404
    except Exception as e:
        log.exception("stop_run failed")
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/live/status")
def status():
    return jsonify({"runs": engine.status()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8800)))
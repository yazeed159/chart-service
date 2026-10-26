"""
client_errors.py
POST /client-errors -- the browser reports an uncaught error here so you can see it in Render's log
stream instead of only on the device that hit it (the toast's "Copy details" button was the only
other way). Each report is written as ONE json log line prefixed "client-error", so it is easy to
search in Render: filter for `client-error`.

  Headers: Authorization: Bearer <supabase access token>   (required)
  Body:    {"t","kind","msg","where","page","stack","ua"}   -- every field optional, trimmed below
  -> 204 on success, 400 bad body, 401 no/invalid token, 413 too large, 429 too many reports

Guards, because this is an open-ended write endpoint: the body is capped (MAX_BODY_BYTES) and every
field truncated; an IP is limited BEFORE the token is checked (so junk can't make us call Supabase),
and a user is limited after. Counters are in memory, per process -- fine for the single-process
Render deploy, and a restart only resets them. The user id is logged as its first 8 characters.
"""

import json
import logging
import re
import threading
import time
from collections import deque

from flask import Blueprint, jsonify, request

from supabase_auth import resolve_user_id

log = logging.getLogger("client_errors")
bp = Blueprint("client_errors", __name__)

MAX_BODY_BYTES = 8 * 1024
IP_LIMIT, IP_WINDOW_S = 60, 600        # 60 reports / 10 min per IP
USER_LIMIT, USER_WINDOW_S = 30, 3600   # 30 reports / hour per user
FIELD_MAX = {"t": 40, "kind": 24, "msg": 300, "where": 200, "page": 120, "stack": 800, "ua": 200}

_lock = threading.Lock()
_hits: dict[str, deque] = {}
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _allow(key: str, limit: int, window_s: int) -> bool:
    now = time.monotonic()
    with _lock:
        q = _hits.setdefault(key, deque())
        while q and now - q[0] > window_s:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(_hits) > 5000:   # bound memory: drop keys whose newest hit has expired
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > window_s]:
                _hits.pop(k, None)
        return True


def _clean(value, limit: int) -> str:
    return _CTRL.sub(" ", str(value if value is not None else ""))[:limit]


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() or request.remote_addr or "?")[:64]


@bp.route("/client-errors", methods=["POST"])
def client_errors():
    if (request.content_length or 0) > MAX_BODY_BYTES:
        return jsonify({"error": "report too large"}), 413
    if not _allow("ip:" + _client_ip(), IP_LIMIT, IP_WINDOW_S):
        return jsonify({"error": "too many reports"}), 429
    user_id = resolve_user_id(request.headers.get("Authorization"))
    if not user_id:
        return jsonify({"error": "missing or invalid Authorization token"}), 401
    if not _allow("user:" + user_id, USER_LIMIT, USER_WINDOW_S):
        return jsonify({"error": "too many reports"}), 429
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "expected a JSON object"}), 400
    record = {k: _clean(body.get(k), n) for k, n in FIELD_MAX.items()}
    record["user"] = str(user_id)[:8]
    log.warning("client-error %s", json.dumps(record, ensure_ascii=False))
    return "", 204

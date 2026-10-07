"""
publish.py
Part 2 of the daily IBKR Flex sync pipeline -- the Supabase publish branch
that was left out of daily_sync.py's first pass:

  Prepare Trade Payloads -> Skip? (No Chart Data) -> Publish OK? ->
  Format Individual Failed / Collect Confirmed Trades -> Any Confirmed
  Trades? -> Supabase: Get User Trades -> Build Merged Trades Rows ->
  Supabase: Upsert Trades -> Index Update OK? -> Format Confirmed As
  Published / Format Confirmed As Index Failed -> Combine All Results

Deliberately NOT ported (per current instructions -- revisit later if you
change your mind):
  - Google Sheets ("Create Daily Sheet1" / "Lookup Sheet Info1" /
    "Build Sheet Update1" / "Write Sheet Update1") -- skipped entirely.
  - "Notify Trade Publish Failed" / "Notify Chart Data Skipped" Telegram
    alerts -- skipped; failures still get logged server-side (see Render
    logs) so nothing is silently lost, just not pushed to Telegram.
  - "send failed message" (Flex request failed alert, already ported in
    daily_sync.py's request_flex_statement) -- also skipped per the same
    instruction; that call site now just logs instead of calling
    _telegram_send.

BUG FIXED while porting: the n8n "Build Merged Trades Rows" node's code
was written for a GitHub Contents API response (`res.body.sha` /
`res.body.content`, base64-encoded JSON) -- leftover from before this
pipeline's trade index moved into Supabase's `trades` table. But it's
wired to "Supabase: Get User Trades", which returns a plain REST array,
so `res.body.sha`/`res.body.content` were always undefined, `rows` was
always `[]`, and running `equity_after` was silently computed across only
THIS RUN's newly-confirmed trades instead of the user's full trade
history -- so equity_after would reset/drift every run instead of
accumulating correctly. Fixed here: the existing rows fetched from
Supabase ARE the full merge base (no separate content/sha decode step
needed), confirmed rows are merged in by id, the whole set is re-sorted
and equity recomputed from scratch, matching what the node's own comments
clearly intended.

Env vars (reuses SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY from
daily_sync.py -- no new ones).
"""

from __future__ import annotations

import os
import re
import time
import logging

import requests

log = logging.getLogger("chart_service.publish")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# "Supabase: Upsert Trade Detail"'s options.batching: batchSize 1, batchInterval 750ms.
DETAIL_PUBLISH_PACING_S = 0.75


def _as_int(v):
    """Supabase int columns reject '167.0'; coerce whole-number floats."""
    if v is None or v == "":
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Prepare Trade Payloads
# ---------------------------------------------------------------------------

def _normalize_better_time(raw, trade_date, fallback_time) -> str:
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?", str(raw or ""), re.I)
    if not m:
        return f"{trade_date}T{fallback_time}"
    hh = int(m.group(1))
    mm, ss = m.group(2), m.group(3) or "00"
    ampm = (m.group(4) or "").upper()
    if ampm == "PM" and hh < 12:
        hh += 12
    if ampm == "AM" and hh == 12:
        hh = 0
    return f"{trade_date}T{hh:02d}:{mm}:{ss}"


def prepare_trade_payload(trade: dict) -> dict:
    """Port of 'Prepare Trade Payloads'. Returns either
    {_skip: True, status: 'skipped', id, symbol, trade_date, user_id,
     broker_account_id} when the final chart never produced
    indicators/bars, or {_skip: False, id, user_id, broker_account_id,
    detail, index_row}."""
    trade_date = trade.get("Trade Date")
    date_no_dash = (trade_date or "").replace("-", "")
    entry_no_colon = (trade.get("Entry Time") or "").replace(":", "")
    trade_id = f"{trade.get('Symbol')}-{date_no_dash}-{entry_no_colon}"

    indicators = trade.get("_final_indicators")
    bars = trade.get("_final_bars")
    user_id = trade.get("_user_id")
    broker_account_id = trade.get("_broker_account_id")
    account_id = trade.get("_account_id")

    if not indicators or not bars:
        return {
            "_skip": True, "status": "skipped", "id": trade_id,
            "symbol": trade.get("Symbol"), "trade_date": trade_date,
            "user_id": user_id, "broker_account_id": broker_account_id,
        }

    better_entry = None
    if trade.get("Better Entry Price"):
        better_entry = {
            "price": trade["Better Entry Price"],
            "time": _normalize_better_time(trade.get("Better Entry Time"), trade_date, trade.get("Entry Time")),
            "reason": trade.get("Better Entry Reason") or "",
            "how_to_know": trade.get("Better Entry How To Know") or "",
        }
    better_exit = None
    if trade.get("Better Exit Price"):
        better_exit = {
            "price": trade["Better Exit Price"],
            "time": _normalize_better_time(trade.get("Better Exit Time"), trade_date, trade.get("Exit Time")),
            "reason": trade.get("Better Exit Reason") or "",
            "how_to_know": trade.get("Better Exit How To Know") or "",
        }

    detail = {
        "id": trade_id, "user_id": user_id, "broker_account_id": broker_account_id,
        "symbol": trade.get("Symbol"), "side": (trade.get("Side") or "Long").lower(),
        "trade_date": trade_date, "entry_time": trade.get("Entry Time"), "exit_time": trade.get("Exit Time"),
        "entry_price": float(trade["Entry Price"]) if trade.get("Entry Price") is not None else None,
        "exit_price": float(trade["Exit Price"]) if trade.get("Exit Price") is not None else None,
        "entry_indicator": trade.get("Entry Indicator") or "", "exit_indicator": trade.get("Exit Indicator") or "",
        "shares": _as_int(trade.get("No. of Shares")), "time_in_trade": trade.get("Time in Trade"),
        "pnl_before_comm": trade.get("P&L Before Comm"), "commission": trade.get("Commission"),
        "pnl_after_comm": trade.get("P&L After Comm"), "win": trade.get("Result") == "Win",
        "fill_count": _as_int(trade.get("Fill Count")) or 1, "fills": trade.get("Fills") or [],
        "verdict": trade.get("LLM Reasoning") or "", "setup_type": trade.get("Setup Type") or "",
        "better_entry": better_entry, "better_exit": better_exit,
        "suggested_stop": trade.get("Suggested Stop"), "suggested_target": trade.get("Suggested Target"),
        "risk_reward": trade.get("Risk:Reward") or "", "walk_away_rule": trade.get("Walk-Away Rule") or "",
        "lessons": trade.get("Lessons") or [],
        "symbol_info": {
            "name": trade.get("Symbol Name") or "", "country": trade.get("Symbol Country") or "",
            "sector": trade.get("Symbol Sector") or "", "description": trade.get("Symbol Description") or "",
        },
        "indicators": indicators, "bars": bars,
    }

    index_row = {
        "id": trade_id, "user_id": user_id, "broker_account_id": broker_account_id,
        "symbol": detail["symbol"], "side": detail["side"], "trade_date": detail["trade_date"],
        "entry_time": detail["entry_time"], "exit_time": detail["exit_time"],
        "entry_price": detail["entry_price"], "exit_price": detail["exit_price"], "shares": detail["shares"],
        "pnl_before_comm": detail["pnl_before_comm"], "commission": detail["commission"],
        "pnl_after_comm": detail["pnl_after_comm"], "win": detail["win"], "setup_type": detail["setup_type"],
        "fill_count": detail["fill_count"],
        "verdict_label": trade.get("Verdict") or "",
        "better_entry_price": better_entry["price"] if better_entry else None,
        "better_exit_price": better_exit["price"] if better_exit else None,
        "lesson_tags": [l.get("tag") for l in (detail["lessons"] or []) if isinstance(l, dict) and l.get("tag")],
        "sector": detail["symbol_info"]["sector"] or None,
        "country": detail["symbol_info"]["country"] or None,
        "avg_volume_tag": indicators.get("avg_volume_tag"),
        "rvol_tag": indicators.get("rvol_tag"),
        "float_tag": indicators.get("float_tag"),
        "relative_volume": indicators.get("relative_volume"),
    }

    # Only send account_id when we actually have one: before 001_accounts.sql
    # has been run the column doesn't exist, and PostgREST rejects unknown keys.
    if account_id:
        index_row["account_id"] = account_id

    return {
        "_skip": False, "id": trade_id, "user_id": user_id, "broker_account_id": broker_account_id,
        "detail": detail, "index_row": index_row,
    }


# ---------------------------------------------------------------------------
# Supabase: Upsert Trade Detail
# ---------------------------------------------------------------------------

def _publish_trade_detail(payload: dict) -> bool:
    """POSTs one trade_details row. Returns True on 200/201 (mirrors
    'Publish OK?'). Never raises -- a network error is treated as a
    failed publish, same as neverError:true + statusCode check in n8n."""
    detail = payload["detail"]
    body = [{
        "trade_id": payload["id"], "user_id": payload["user_id"],
        "time_in_trade": detail["time_in_trade"], "verdict": detail["verdict"],
        "fill_count": detail["fill_count"], "fills": detail["fills"],
        "indicators": detail["indicators"], "bars": detail["bars"],
        "better_entry": detail["better_entry"], "better_exit": detail["better_exit"],
        "suggested_stop": detail["suggested_stop"], "suggested_target": detail["suggested_target"],
        "risk_reward": detail["risk_reward"], "walk_away_rule": detail["walk_away_rule"],
        "lessons": detail["lessons"], "symbol_info": detail["symbol_info"],
    }]
    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/trade_details",
            params={"on_conflict": "user_id,trade_id"},
            headers={**_supabase_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
            json=body, timeout=30,
        )
    except requests.RequestException as e:
        log.error("trade_details publish failed for %s: %s", payload["id"], e)
        payload["_error"] = f"trade_details request failed: {type(e).__name__}: {e}"[:300]
        return False
    if resp.status_code not in (200, 201):
        log.error("trade_details publish failed for %s: HTTP %s %s", payload["id"], resp.status_code, resp.text[:500])
        payload["_error"] = f"trade_details HTTP {resp.status_code}: {resp.text[:250]}"
        return False
    return True


# ---------------------------------------------------------------------------
# Supabase: Get User Trades -> merge -> Supabase: Upsert Trades
# ---------------------------------------------------------------------------

def _get_user_trades(user_id) -> list[dict]:
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/trades",
        params={"user_id": f"eq.{user_id}", "select": "*", "order": "trade_date.asc,entry_time.asc"},
        headers=_supabase_headers(), timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def default_live_account_id(user_id):
    """The user's oldest active real account (the "Main account" the SQL
    migration / frontend creates), or None. Used when a trade arrives
    without an explicit account."""
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/accounts",
            params={"user_id": f"eq.{user_id}", "kind": "eq.live", "status": "eq.active",
                    "select": "id", "order": "created_at.asc", "limit": "1"},
            headers=_supabase_headers(), timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        return rows[0]["id"] if rows else None
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        # accounts table missing (migration not run yet) or transient error:
        # fall back to "no account", i.e. the pre-accounts behavior.
        log.warning("default_live_account_id failed for user %s: %s", user_id, e)
        return None


def resolve_account_id(user_id, requested):
    """Validate a client-supplied account id: it must be one of THIS user's
    real accounts. Anything else (missing, someone else's, a paper account)
    falls back to the user's default real account."""
    if requested:
        try:
            resp = requests.get(
                f"{SUPABASE_URL}/rest/v1/accounts",
                params={"id": f"eq.{requested}", "user_id": f"eq.{user_id}", "kind": "eq.live", "select": "id"},
                headers=_supabase_headers(), timeout=15,
            )
            if resp.status_code == 200 and resp.json():
                return requested
        except (requests.RequestException, ValueError):
            pass
    return default_live_account_id(user_id)


def _merge_and_upsert_trades(user_id, confirmed_index_rows: list[dict], err_out: list | None = None) -> bool:
    """Fetches the user's existing trades, merges in the newly-confirmed
    rows by id, recomputes running equity_after PER ACCOUNT over each
    account's full date-sorted history, and upserts the merged set. Returns
    True on 200/201 (mirrors 'Index Update OK?'). See module docstring for
    the bug this replaces."""
    try:
        existing = _get_user_trades(user_id)
    except requests.RequestException as e:
        log.error("Get User Trades failed for user %s: %s", user_id, e)
        if err_out is not None:
            err_out.append(f"reading existing trades failed: {type(e).__name__}: {e}"[:300])
        return False

    default_acct = default_live_account_id(user_id)
    by_id = {row["id"]: row for row in existing}
    for row in confirmed_index_rows:
        if not row.get("account_id"):
            # keep the account a re-synced trade already had; else the default
            # (skipped entirely if neither exists, e.g. migration not run yet)
            keep = (by_id.get(row["id"]) or {}).get("account_id") or default_acct
            if keep:
                row["account_id"] = keep
        by_id[row["id"]] = row
    merged = sorted(by_id.values(), key=lambda r: (r.get("trade_date") or "", r.get("entry_time") or ""))

    equity_by_account = {}
    for row in merged:
        key = row.get("account_id") or default_acct
        equity_by_account[key] = equity_by_account.get(key, 0.0) + (row.get("pnl_after_comm") or 0)
        row["equity_after"] = round(equity_by_account[key], 2)

    # PostgREST rejects a bulk upsert unless EVERY object has the same keys
    # (PGRST102 "All object keys must match"). Existing rows come back from
    # `select=*` with every DB column; new rows only carry the index_row
    # fields. Project all rows onto one shared column set: the new rows'
    # fields + equity_after (+ account_id when the column exists). Columns
    # outside that set (created_at etc.) are simply not sent, so the upsert
    # leaves them untouched.
    cols = {"id", "user_id", "equity_after"}
    for r in confirmed_index_rows:
        cols.update(r.keys())
    if any("account_id" in r for r in merged):
        cols.add("account_id")
    merged = [{c: r.get(c) for c in cols} for r in merged]

    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/trades",
            params={"on_conflict": "user_id,id"},
            headers={**_supabase_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
            json=merged, timeout=30,
        )
    except requests.RequestException as e:
        log.error("Upsert Trades failed for user %s: %s", user_id, e)
        if err_out is not None:
            err_out.append(f"trades upsert request failed: {type(e).__name__}: {e}"[:300])
        return False
    if resp.status_code not in (200, 201):
        log.error("Upsert Trades failed for user %s: HTTP %s %s", user_id, resp.status_code, resp.text[:500])
        if err_out is not None:
            err_out.append(f"trades upsert HTTP {resp.status_code}: {resp.text[:250]}")
        return False
    return True


# ---------------------------------------------------------------------------
# Orchestration -- Combine All Results
# ---------------------------------------------------------------------------

def publish_trades(trades: list[dict], on_event=None) -> list[dict]:
    """Entry point -- takes the enriched trades for ONE broker account
    (the same grouping 'Loop Broker Accounts' / 'Collect Confirmed
    Trades' operated on, since equity_after is only meaningful merged
    per-user) and runs them through prepare -> merge index -> upsert
    trades -> publish detail (index first: trade_details has a foreign key
    to trades). Returns a list of {status: 'published'|'failed'|
    'skipped', id, ...} per trade, mirroring 'Combine All Results'.

    on_event (optional): callable(trade_id, state, reason) invoked as each
    trade moves through publishing ('publishing', 'saved', 'skipped',
    'failed', 'published') so a caller (import_routes) can show live
    per-trade status. Exceptions from the callback are swallowed."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set")

    results = []
    user_id = None

    def _emit(trade_id, state, reason=""):
        if on_event:
            try:
                on_event(trade_id, state, reason)
            except Exception as e:  # never let UI reporting break publishing
                log.warning("publish on_event callback failed (ignored): %s", e)

    # --- A) prepare every trade; trades with no chart data are skipped ---
    payloads = []
    for trade in trades:
        payload = prepare_trade_payload(trade)
        if payload["_skip"]:
            log.warning("skipping publish for %s -- final chart had no indicators/bars", payload["id"])
            reason = trade.get("_chart_error") or "no chart data (price bars/indicators unavailable for this symbol/date)"
            results.append({"status": "skipped", "id": payload["id"], "reason": reason})
            _emit(payload["id"], "skipped", reason)
            continue
        user_id = payload["user_id"] or user_id
        payloads.append(payload)

    if not payloads:
        return results

    # --- B) trades index FIRST ---
    # trade_details has a foreign key (user_id, trade_id) -> trades(user_id, id),
    # so Postgres rejects a detail row whose trade row doesn't exist yet
    # (HTTP 409, code 23503). The index row therefore has to be written
    # before the detail row.
    index_errors: list[str] = []
    if not _merge_and_upsert_trades(user_id, [p["index_row"] for p in payloads], index_errors):
        reason = "trades index update failed: " + (index_errors[0] if index_errors else "unknown error")
        for p in payloads:
            results.append({"status": "failed", "id": p["id"], "reason": reason})
            _emit(p["id"], "failed", reason)
        return results
    for p in payloads:
        _emit(p["id"], "saved", "trade row saved, saving chart + verdict details")

    # --- C) detail rows, one at a time ---
    for i, payload in enumerate(payloads):
        if i > 0:
            time.sleep(DETAIL_PUBLISH_PACING_S)
        _emit(payload["id"], "publishing")
        if _publish_trade_detail(payload):
            results.append({"status": "published", "id": payload["id"]})
            _emit(payload["id"], "published")
        else:
            # The trade row from step B stays; re-running the import retries
            # the detail (the already-imported check looks at trade_details).
            reason = payload.get("_error") or "trade detail publish failed"
            results.append({"status": "failed", "id": payload["id"], "reason": reason})
            _emit(payload["id"], "failed", reason)

    return results

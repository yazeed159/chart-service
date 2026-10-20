"""
daily_sync.py
The daily IBKR Flex sync pipeline -- replaces the "Daily 8PM Egypt
Trigger" through "Combine All Results" span of the n8n workflow:

  Get Broker Accounts -> Loop Broker Accounts -> Send Flex Request1 ->
  Parse Send Response1 -> Send Successful?1 -> Get Statement1 ->
  Parse Statement1 -> Report Ready?1 -> (Wait & Retry1 loop) ->
  Extract & Match Trades1 -> Generate Chart -> Summarize Indicators For
  Verdict -> Vision LLM Analysis -> Parse Verdict & Attach Chart ->
  Generate Final Chart -> Attach Final Chart -> [publish.py:] Prepare
  Trade Payloads -> Supabase publish -> Combine All Results

Publishing to Supabase (trade_details + trades, with the running
equity_after recompute) lives in publish.py -- see that file's docstring,
including a bug it fixes along the way (equity_after was being computed
across only the newest batch instead of the user's full history).

Deliberately NOT ported, per current instructions:
  - Google Sheets ("Daily Run? (Skip Sheet Log for CSV Import)" /
    "Build Sheet Update1" / "Write Sheet Update1" / "Create Daily Sheet1" /
    "Lookup Sheet Info1") -- skipped entirely.
  - "Build Daily Summary" / "Send Telegram Summary" -- skipped for now too;
    add back later if you want an end-of-run Telegram digest.
  - The Telegram "Flex request failed" alert -- see the comment at its old
    call site below; it now just logs instead.

run_daily_sync() is the entry point; a Flask route below fires it in a
background thread (same pattern as backtest_import_routes.py) and writes
the run's results (per-account list of {status, id, ...} from
publish.py, same shape as "Combine All Results") to a local JSON file
under DAILY_SYNC_RUNS_DIR so you can inspect/diff it against the n8n
run's own output before fully cutting over (see "Decommission n8n once
parity is confirmed" in the migration plan).

Register from chart_service.py the same way as the other blueprints:

    from daily_sync import bp as daily_sync_bp
    app.register_blueprint(daily_sync_bp)

Env vars (new):
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
      - to read broker_accounts (id, user_id, ibkr_flex_token,
        ibkr_flex_query_id, telegram_chat_id -- same columns "Get Broker
        Accounts" / "Loop Broker Accounts" read off each row)
  TELEGRAM_BOT_TOKEN
      - only used here for the "Flex request failed" alert (mirrors "send
        failed message"); the daily summary message belongs to part 2
  GEMINI_API_KEY / GEMINI_MODEL
      - reused from ai_routes.py -- this module imports GEMINI_URL /
        GEMINI_API_KEY / _call_gemini / _extract_text from there lazily,
        same "doesn't matter which file loads first" pattern as everywhere
        else in this service
  DAILY_SYNC_RUNS_DIR
      - defaults to ./daily_sync_runs -- where each run's enriched-trades
        JSON is written for inspection

IMPORTANT: while porting this, "Get Statement1" in the n8n export had a
live IBKR Flex token hardcoded in its query params (instead of reading
$json.ibkr_flex_token like "Send Flex Request1" did) -- looked like a
leftover from testing. This module always reads the token from the
broker_accounts row for both calls. Worth rotating that token since it's
sitting in plaintext in the workflow export.
"""

from __future__ import annotations

import os
import time
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

import requests
from flask import Blueprint, jsonify

import xml.etree.ElementTree as ET
from flex_xml import parse_flex_xml
from trade_matching import (fifo_match_and_merge, parse_flex_executions, parse_flex_fees,
                            parse_csv_executions, parse_csv_fees, read_csv_sections)
from publish import publish_trades
import symbol_info_store

log = logging.getLogger("chart_service.daily_sync")
bp = Blueprint("daily_sync", __name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

FLEX_SEND_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
FLEX_GET_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
FLEX_USER_AGENT = "Python/3.4.1"  # IBKR's Flex Web Service is picky about this -- matches the n8n nodes verbatim

# "Wait & Retry1" was a 20s wait, retried indefinitely until Report Ready?1
# passed. Kept finite here so a genuinely stuck statement can't hang a
# background thread forever -- 30 tries * 20s = 10 minutes, generously
# past IBKR's typical generation time.
STATEMENT_POLL_INTERVAL_S = 20
STATEMENT_POLL_MAX_TRIES = 30

# "Wait" (60s) -> loops back to "Send Flex Request1" on a failed send.
# Kept finite for the same reason -- the original graph retried forever.
SEND_RETRY_INTERVAL_S = 60
SEND_RETRY_MAX_TRIES = 5

# "Generate Chart" / "Generate Final Chart" batching.batchInterval: 13000 / 0
# -- the first chart call per trade is paced 13s from the previous trade's
# work (shared Polygon rate limit), the final (post-verdict) call isn't
# separately paced since it follows right after the same trade's Gemini call.
CHART_PACING_SECONDS = 13
ACCOUNT_PAUSE_SECONDS = 5   # between accounts: IBKR throttles Flex requests
# Cost guards. A trade is NEVER saved without its chart. One whose chart can't be built is not
# saved; it is retried on every run for the first FAST_RETRIES failures, then at most once per
# RETRY_COOLDOWN_HOURS (so a symbol Polygon has no data for doesn't burn API calls every run, but
# a temporary outage still heals on its own).
FAST_RETRIES = int(os.environ.get("SYNC_FAST_RETRIES", "2"))
RETRY_COOLDOWN_HOURS = float(os.environ.get("SYNC_RETRY_COOLDOWN_HOURS", "20"))
# At most this many NEW trades get a chart + AI verdict per account per run; the rest wait for
# the next run (they are not saved yet, so they are picked up again automatically).
MAX_NEW_TRADES_PER_RUN = int(os.environ.get("SYNC_MAX_NEW_TRADES_PER_RUN", "15"))
ATTEMPTS_KEY = "sync_attempts"   # user_kv: {trade_id: {"n": failed chart attempts, "last": ISO time of the latest, "err": why}}
REPORT_KEY = "sync_report"       # user_kv: {"accounts": {account_id: {at, open_positions, no_chart, skipped, deferred}}} -- shown on the Accounts page
GEMINI_TIMEOUT_S = 30  # matches Vision LLM Analysis's node timeout

DAILY_SYNC_RUNS_DIR = Path(os.environ.get("DAILY_SYNC_RUNS_DIR", "daily_sync_runs"))


# ---------------------------------------------------------------------------
# Telegram (minimal -- just the failure alert; the daily summary is part 2)
# ---------------------------------------------------------------------------

def _telegram_send(chat_id, text: str):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        log.warning("Telegram not configured (or no chat_id) -- would have sent: %s", text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning("Telegram send failed: %s", e)


# ---------------------------------------------------------------------------
# Broker accounts (Supabase)
# ---------------------------------------------------------------------------

def _sb_headers() -> dict:
    return {"apikey": SUPABASE_SERVICE_ROLE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"}


def _sb_get(table: str, params: dict) -> list[dict]:
    resp = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", params=params, headers=_sb_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _sb_get_all(table: str, params: dict, page: int = 1000) -> list[dict]:
    """Every row, not just the first page: Supabase caps one response at 1000 rows, and a silently
    truncated list here would make old trades look new (and re-run the AI on them)."""
    rows, start = [], 0
    while True:
        resp = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", params=params,
                            headers={**_sb_headers(), "Range-Unit": "items", "Range": f"{start}-{start + page - 1}"}, timeout=30)
        resp.raise_for_status()
        chunk = resp.json()
        rows.extend(chunk)
        if len(chunk) < page:
            return rows
        start += page


def get_broker_accounts() -> list[dict]:
    """The accounts to sync. Each active account with its own Flex token + query ID
    (table account_flex, edited on the site's Accounts page) is one entry. Users with NO
    account_flex rows fall back to the old single-token broker_accounts row, so nothing
    stops syncing before someone has re-entered their keys per account."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set")
    legacy = _sb_get("broker_accounts", {"select": "*"})
    legacy_by_user = {r.get("user_id"): r for r in legacy}

    try:
        flex_rows = _sb_get("account_flex", {"select": "*"})
        acct_rows = _sb_get("accounts", {"select": "id,user_id,name,status"})
    except requests.RequestException as e:
        # table not created yet: behave exactly as before
        log.warning("account_flex/accounts not readable (%s) -- using broker_accounts only", e)
        return legacy

    active = {a["id"]: a for a in acct_rows if a.get("status") != "archived"}
    out, users_with_flex = [], set()
    for r in flex_rows:
        users_with_flex.add(r.get("user_id"))
        a = active.get(r.get("account_id"))
        if not a or not r.get("ibkr_flex_token") or not r.get("ibkr_flex_query_id"):
            continue  # archived account, or keys not filled in yet
        out.append({
            "id": a["id"], "account_id": a["id"], "user_id": a.get("user_id") or r.get("user_id"),
            # trades.broker_account_id is a FK to broker_accounts, NOT accounts: only send the
            # user's legacy broker_accounts id (or None), never the accounts.id above.
            "broker_account_id": (legacy_by_user.get(r.get("user_id")) or {}).get("id"),
            "name": a.get("name"), "ibkr_flex_token": r["ibkr_flex_token"],
            "ibkr_flex_query_id": r["ibkr_flex_query_id"], "ibkr_account_ref": r.get("ibkr_account_ref"),
            "telegram_chat_id": (legacy_by_user.get(r.get("user_id")) or {}).get("telegram_chat_id"),
            "_source": "account_flex",
        })
    out.extend(r for r in legacy if r.get("user_id") not in users_with_flex)
    return out


def existing_trade_ids(user_id) -> set:
    """Trade ids this user already has saved WITH their detail row (a trade whose detail
    failed to save last time is retried)."""
    ids = {r["id"] for r in _sb_get_all("trades", {"user_id": f"eq.{user_id}", "select": "id", "order": "id.asc"})}
    have_detail = {r["trade_id"] for r in _sb_get_all("trade_details", {"user_id": f"eq.{user_id}", "select": "trade_id", "order": "trade_id.asc"})}
    return ids & have_detail


def report_status(account: dict, status: str, message: str):
    """Write last_sync_* back so the chip on the Accounts page shows what happened."""
    if account.get("_source") != "account_flex":
        return
    try:
        resp = requests.patch(
            f"{SUPABASE_URL}/rest/v1/account_flex",
            params={"account_id": f"eq.{account['account_id']}"},
            json={"last_sync_at": datetime.utcnow().isoformat() + "Z", "last_sync_status": status,
                  "last_sync_message": (message or "")[:300]},
            headers={**_sb_headers(), "Content-Type": "application/json"}, timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("could not write sync status for account %s: %s", account.get("id"), e)


def load_attempts(user_id) -> dict:
    try:
        rows = _sb_get("user_kv", {"user_id": f"eq.{user_id}", "key": f"eq.{ATTEMPTS_KEY}", "select": "value"})
        v = rows[0]["value"] if rows else {}
        v = json.loads(v) if isinstance(v, str) else v
        return {k: (n if isinstance(n, dict) else {"n": int(n), "last": None}) for k, n in (v or {}).items()}
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("could not read %s for user %s (%s) -- treating as none", ATTEMPTS_KEY, user_id, e)
        return {}


def save_attempts(user_id, attempts: dict):
    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/user_kv", params={"on_conflict": "user_id,key"},
            json=[{"user_id": user_id, "key": ATTEMPTS_KEY, "value": json.dumps(attempts)}],
            headers={**_sb_headers(), "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("could not save %s for user %s: %s", ATTEMPTS_KEY, user_id, e)


def retry_due(entry: dict | None) -> bool:
    if not entry or int(entry.get("n", 0)) < FAST_RETRIES or not entry.get("last"):
        return True
    try:
        last = datetime.fromisoformat(str(entry["last"]).replace("Z", ""))
    except ValueError:
        return True
    return (datetime.utcnow() - last).total_seconds() >= RETRY_COOLDOWN_HOURS * 3600


def next_retry_of(entry: dict | None):
    """When this stuck trade will be tried again: None = on the very next run, else an ISO time
    (the end of its cooldown)."""
    if not entry or int(entry.get("n", 0)) < FAST_RETRIES or not entry.get("last"):
        return None
    try:
        last = datetime.fromisoformat(str(entry["last"]).replace("Z", ""))
    except ValueError:
        return None
    from datetime import timedelta
    return (last + timedelta(hours=RETRY_COOLDOWN_HOURS)).isoformat() + "Z"


def load_report(user_id) -> dict:
    try:
        rows = _sb_get("user_kv", {"user_id": f"eq.{user_id}", "key": f"eq.{REPORT_KEY}", "select": "value"})
        v = rows[0]["value"] if rows else {}
        v = json.loads(v) if isinstance(v, str) else v
        return v if isinstance(v, dict) and isinstance(v.get("accounts"), dict) else {"accounts": {}}
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("could not read %s for user %s (%s) -- starting fresh", REPORT_KEY, user_id, e)
        return {"accounts": {}}


def save_account_report(account: dict, report: dict):
    """Store this account's sync details in user_kv under `sync_report` (keyed by account id, so one
    user's accounts share the one row). Best effort: a failure here never breaks the sync."""
    user_id, aid = account.get("user_id"), account.get("account_id")
    if not user_id or not aid:
        return   # legacy single-token row: the Accounts page has no account to show it on
    try:
        whole = load_report(user_id)
        whole["accounts"][str(aid)] = report
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/user_kv", params={"on_conflict": "user_id,key"},
            json=[{"user_id": user_id, "key": REPORT_KEY, "value": json.dumps(whole)}],
            headers={**_sb_headers(), "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("could not save %s for account %s: %s", REPORT_KEY, aid, e)


def _fmt_fill_time(raw) -> str:
    """'20261012;093005' / '2026-10-12 09:30:05' -> '2026-10-12 09:30' (lenient; '' if unparseable)."""
    d = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]} {d[8:10]}:{d[10:12]}" if len(d) >= 12 else (f"{d[0:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else "")


def statement_info(flex_doc: dict, raw_executions: list, closed_count: int) -> dict:
    """What IBKR actually sent -- the first thing to check when 'my trades are not showing up':
    the period the statement covers and the latest fill in it."""
    stmts = ((flex_doc.get("FlexQueryResponse") or {}).get("FlexStatements") or {}).get("FlexStatement") or []
    st = (stmts[0] if isinstance(stmts, list) and stmts else stmts) or {}
    st = st if isinstance(st, dict) else {}
    times = sorted(_fmt_fill_time(r.get("dateTime")) for r in raw_executions if r.get("dateTime"))
    return {
        "from": str(st.get("fromDate") or ""), "to": str(st.get("toDate") or ""),
        "period": str(st.get("period") or ""), "generated": str(st.get("whenGenerated") or ""),
        "fills": len(raw_executions), "closed": closed_count, "latest_fill": times[-1] if times else "",
    }


def build_report(account: dict, publish_results: list[dict]) -> dict:
    """Everything the sync saw but did NOT turn into a saved trade, for the Accounts page."""
    skipped = [dict(ig) for ig in (account.get("_ignored") or [])]
    for r in publish_results:
        if r.get("status") == "failed":
            skipped.append({"symbol": str(r.get("id") or "").split("-")[0], "reason": f"could not be saved: {r.get('reason') or 'unknown error'}"})
    return {
        "at": datetime.utcnow().isoformat() + "Z",
        "open_positions": account.get("_open") or [],
        "no_chart": account.get("_stuck") or [],
        "skipped": skipped,
        "deferred": int(account.get("_deferred") or 0),
        "statement": account.get("_statement") or {},
    }


def trade_id_of(trade: dict) -> str:
    """Same id publish.py gives the trade: SYMBOL-YYYYMMDD-HHMMSS (entry)."""
    return f"{trade.get('Symbol')}-{(trade.get('Trade Date') or '').replace('-', '')}-{(trade.get('Entry Time') or '').replace(':', '')}"


# ---------------------------------------------------------------------------
# Flex request + wait/retry poll
# ---------------------------------------------------------------------------

def _parse_or_explain(resp) -> dict:
    """parse_flex_xml, but if IBKR (or something in front of it) sent back a
    non-XML body, say what it actually was instead of a bare expat error."""
    try:
        return parse_flex_xml(resp.content)
    except ET.ParseError as e:
        snippet = resp.text[:200].replace("\n", " ").strip()
        raise RuntimeError(
            f"IBKR returned non-XML (HTTP {resp.status_code}, "
            f"{resp.headers.get('Content-Type', 'no content-type')}): {snippet!r} [{e}]"
        ) from e


def _send_flex_request(token: str, query_id: str) -> dict:
    resp = requests.get(
        FLEX_SEND_URL,
        params={"t": token, "q": query_id, "v": "3"},
        headers={"User-Agent": FLEX_USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    return _parse_or_explain(resp)


def _get_statement(token: str, reference_code: str) -> dict:
    resp = requests.get(
        FLEX_GET_URL,
        params={"t": token, "q": reference_code, "v": "3"},
        headers={"User-Agent": FLEX_USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    # IBKR sends the statement in whatever Delivery Format the Flex Query is set to. XML is parsed
    # as before; CSV (anything not starting with '<') is passed along as raw text and handled by
    # the CSV parsers in trade_matching.py. "Not ready yet" replies are always XML.
    if not resp.text.lstrip("\ufeff \t\r\n").startswith("<"):
        return {"FlexCsv": resp.text.lstrip("\ufeff")}
    return _parse_or_explain(resp)


def request_flex_statement(account: dict) -> dict:
    """Send + wait/retry-poll loop, mirrors "Send Flex Request1" through
    "Report Ready?1" / "Wait & Retry1". Returns the parsed, ready
    FlexQueryResponse dict. Raises RuntimeError if the send never
    succeeds or the statement never becomes ready within the retry
    budgets above."""
    token = account.get("ibkr_flex_token")
    query_id = account.get("ibkr_flex_query_id")
    chat_id = account.get("telegram_chat_id")
    if not token or not query_id:
        raise RuntimeError(f"broker_account {account.get('id')} is missing ibkr_flex_token/ibkr_flex_query_id")

    send_doc = None
    for attempt in range(1, SEND_RETRY_MAX_TRIES + 1):
        try:
            doc = _send_flex_request(token, query_id)
        except requests.RequestException as e:
            log.warning("Flex send request failed for account %s (attempt %d): %s", account.get("id"), attempt, e)
            doc = None

        status = ((doc or {}).get("FlexStatementResponse") or {}).get("Status")
        if status == "Success":
            send_doc = doc
            break

        log.warning(
            "Flex send not successful for account %s (attempt %d/%d): status=%s",
            account.get("id"), attempt, SEND_RETRY_MAX_TRIES, status,
        )
        # Telegram alert on Flex-send failure intentionally skipped for now
        # (matches 'send failed message' in n8n) -- this still logs above,
        # it just doesn't page Telegram. Revisit if you want it back.
        if attempt < SEND_RETRY_MAX_TRIES:
            time.sleep(SEND_RETRY_INTERVAL_S)

    if send_doc is None:
        raise RuntimeError(f"Flex SendRequest never succeeded for account {account.get('id')} after {SEND_RETRY_MAX_TRIES} tries")

    reference_code = send_doc["FlexStatementResponse"]["ReferenceCode"]

    for attempt in range(1, STATEMENT_POLL_MAX_TRIES + 1):
        stmt_doc = _get_statement(token, reference_code)
        if stmt_doc.get("FlexQueryResponse") or stmt_doc.get("FlexCsv"):
            return stmt_doc
        log.info(
            "Flex statement not ready yet for account %s (attempt %d/%d)",
            account.get("id"), attempt, STATEMENT_POLL_MAX_TRIES,
        )
        if attempt < STATEMENT_POLL_MAX_TRIES:
            time.sleep(STATEMENT_POLL_INTERVAL_S)

    raise RuntimeError(
        f"Flex statement for account {account.get('id')} never became ready after "
        f"{STATEMENT_POLL_MAX_TRIES * STATEMENT_POLL_INTERVAL_S}s of polling"
    )


# ---------------------------------------------------------------------------
# Per-trade: chart -> indicator summary -> Gemini verdict -> final chart
# ---------------------------------------------------------------------------

# Cap how many bar rows we ever hand to the model -- a normal
# scalp/day-trade display window is well under this, but a very long
# hold shouldn't blow up the prompt.
_MAX_BAR_ROWS_FOR_PROMPT = 200


def _bar_table_for_prompt(chart: dict | None) -> str:
    """Render the display-window bars as a compact time/low/high/close
    table so the model has real, addressable price levels to anchor
    better_entry_time/better_entry_price (and the exit equivalents) to,
    instead of inventing a time and a price as two independent guesses.
    Without this, the model has no way to know what price the stock
    actually traded at away from the single entry-time snapshot above --
    so a "better entry 10 minutes earlier" is pure hallucination for both
    when and at what price, and the two guesses routinely disagree with
    what really happened on the tape (a price that no bar that day ever
    reached, at a time that doesn't line up with it either). That's what
    shows up on the trade page as a better-entry/exit marker floating in
    empty space, disconnected from every candle.
    """
    bars = (chart or {}).get("bars")
    if not isinstance(bars, list) or not bars:
        return ""
    rows = bars[:_MAX_BAR_ROWS_FOR_PROMPT]
    lines = [
        "Minute-by-minute bars for this display window (time, low, high, "
        "close) -- the ONLY price levels that actually traded. Every "
        "better_entry_time/better_exit_time you propose MUST be the exact "
        "timestamp of one of these bars, and the matching "
        "better_entry_price/better_exit_price MUST fall within that bar's "
        "low-high range (inclusive) -- never a price or time this table "
        "doesn't support:",
    ]
    for b in rows:
        t = b.get("t")
        l, h, c = b.get("l"), b.get("h"), b.get("c")
        if t is None or l is None or h is None or c is None:
            continue
        lines.append(f"- {t}: low ${l:.2f}, high ${h:.2f}, close ${c:.2f}")
    if len(bars) > _MAX_BAR_ROWS_FOR_PROMPT:
        lines.append(f"... ({len(bars) - _MAX_BAR_ROWS_FOR_PROMPT} more bars omitted for length)")
    return "\n".join(lines)


def _indicator_summary(chart: dict | None) -> str:
    """Port of 'Summarize Indicators For Verdict'."""
    indicators = (chart or {}).get("indicators")
    if not indicators:
        return (
            "Indicators unavailable (chart service call failed or returned no data) -- "
            "judge this trade on price and time alone."
        )

    def fmt(n, decimals=2):
        return f"{n:.{decimals}f}" if isinstance(n, (int, float)) else "n/a"

    summary = "\n".join([
        "Indicators at entry (ground-truth from the chart service, not read off a chart image):",
        f"- VWAP: ${fmt(indicators.get('vwap_at_entry'))} (entry is {indicators.get('entry_vs_vwap', 'n/a')} VWAP)",
        f"- EMA9: ${fmt(indicators.get('ema9_at_entry'))} (entry is {indicators.get('entry_vs_ema9', 'n/a')} EMA9)",
        f"- EMA20: ${fmt(indicators.get('ema20_at_entry'))} (entry is {indicators.get('entry_vs_ema20', 'n/a')} EMA20)",
        f"- EMA200: ${fmt(indicators.get('ema200_at_entry'))} (entry is {indicators.get('entry_vs_ema200', 'n/a')} EMA200 -- "
        f"longer-term trend context only, not part of the dip-buy/breakout criteria below)",
        f"- MACD: {fmt(indicators.get('macd_at_entry'), 4)}, Signal: {fmt(indicators.get('macd_signal_at_entry'), 4)}, "
        f"Histogram: {fmt(indicators.get('macd_hist_at_entry'), 4)} (prior bar: {fmt(indicators.get('macd_hist_prior_bar'), 4)})",
        f"- Setup tag (rule-based from the candles; red_to_green = entry above the high of the prior red 1-min bar, "
        f"green_over_green_wick = entry above the upper wick of the prior green bar, ema9_dip_buy = bounce off the 9 EMA, chase = right pattern but filled too far above the trigger): "
        f"{indicators.get('setup_type', 'n/a')}",
        f"- Service-suggested stop/target: ${fmt(indicators.get('stop_price'))} / ${fmt(indicators.get('target_price'))} "
        f"(R multiple: {fmt(indicators.get('r_multiple'), 2)})",
        f"- Display window price range: ${fmt(indicators.get('display_price_low'))} - ${fmt(indicators.get('display_price_high'))}",
    ])
    bar_table = _bar_table_for_prompt(chart)
    return f"{summary}\n\n{bar_table}" if bar_table else summary


def _build_verdict_prompt(trade: dict, indicator_summary: str, need_symbol_info: bool = True) -> str:
    """Faithful port of "Vision LLM Analysis"'s prompt text.

    need_symbol_info=False drops the symbol_name/symbol_country/
    symbol_sector/symbol_description fields from the requested JSON
    shape entirely -- used once a symbol's company info is already on
    file (see symbol_info_store.py + process_trade below), since asking
    Gemini to regenerate the same four facts on every trade of an
    already-known symbol was pure repeated prompt/output for no new
    information. First-ever trade on a symbol still asks for them here,
    same call, no extra round trip."""
    symbol = trade.get("Symbol")
    side = trade.get("Side") or "Long"
    entry_price = trade.get("Entry Price")
    entry_time = trade.get("Entry Time")
    exit_price = trade.get("Exit Price")
    exit_time = trade.get("Exit Time")
    symbol_info_schema = (
        ", \"symbol_name\": string (the "
        "company/asset full name), \"symbol_country\": string (country the company is "
        "headquartered or listed in), \"symbol_sector\": string (industry/sector), "
        "\"symbol_description\": string (2-3 sentences on what the company actually does)"
        if need_symbol_info else ""
    )
    return (
        "You are grading this trade against a momentum day-trading playbook modeled on Ross "
        "Cameron's (Warrior Trading) rules: the two entries that count are a DIP BUY (a pullback "
        "to VWAP or the EMA9 inside an already-established uptrend, entered as price reclaims "
        "that level) and a BREAKOUT (a push through a clear recent high on above-average "
        "volume). Good trades in this style respect VWAP as the key intraday line in the sand, "
        "avoid chasing price that's already extended far above VWAP/EMA9, require volume "
        "confirmation on breakouts, and treat a fast, small, predefined stop as non-negotiable "
        "so no single loser can offset multiple winners -- the target is fixed at roughly 2x the "
        "risk rather than guessed after the fact.\n\n"
        f"Trade: {symbol} ({side}) entered at {entry_price} ({entry_time}), exited at "
        f"{exit_price} ({exit_time}).\n\n"
        f"{indicator_summary}\n\n"
        "Based on the setup type implied by this entry/exit and standard playbook criteria (dip "
        "buy = pullback to VWAP/EMA9 inside an uptrend; breakout = push through a recent high on "
        "strong volume): judge whether the entry looks like a reasonable dip buy, breakout, or "
        "neither, and judge the exit against a typical 2x-risk target. Give a stop and target "
        "you'd consider reasonable for a trade like this.\n\n"
        "You MUST always propose a specific better_entry_price AND a specific better_exit_price, "
        "even when the actual entry/exit was already good -- never return null for either one, "
        "and never write \"no better entry/exit\" as the reason. If the actual fill was genuinely "
        "close to optimal, propose the tightest defensible alternative instead (the exact "
        "VWAP/EMA9 tick, the prior 1-minute candle's high or low, or the specific bar where the "
        "reclaim/breakout first confirmed) and say in one sentence why it's marginally better "
        "(less slippage, earlier confirmation, avoided giving back some of the gain, etc.). "
        "better_entry_time and better_exit_time MUST each be copied EXACTLY from one of the "
        "timestamps in the minute-bar table above -- never a rounded, invented, or approximate "
        "time -- and better_entry_price/better_exit_price MUST fall within that same bar's "
        "low-high range. Do not propose a price that no bar in the table actually reached, even "
        "if it looks like a plausible round number. Every "
        "better_entry_reason and better_exit_reason must cite a specific number from the "
        "indicator_summary or the trade's own price/time data above -- never a generic line like "
        "\"entry could have been earlier\" with no number attached.\n\n"
        "Finish with one concrete walk_away_rule the trader can use in the moment next time (e.g. "
        "a specific price or a specific structural signal, not a vague reminder).\n\n"
        "For every better_entry_price and better_exit_price you propose, also give a "
        "how_to_know: the exact observable signal available in real time (a specific indicator "
        "level, price relationship, or bar condition from the data above -- e.g. \"price reclaims "
        "EMA9 at 7.14 on rising volume\") that would have told the trader to act there, not a "
        "hindsight description. This how_to_know pairing is specific to the better-entry/exit "
        "call -- do NOT add one to the lessons below; a lesson is a retrospective note about "
        "the trade, not a real-time signal to watch for.\n\n"
        "Separately from the better-entry/exit analysis, also identify entry_indicator and "
        "exit_indicator for the ACTUAL fill the trader took: describe, using the "
        "indicator_summary/bar data above, the specific observable signal that was present at "
        "the actual entry price/time and at the actual exit price/time -- i.e. what a trader "
        "watching the tape in real time would have pointed to as the reason to act right there "
        "(e.g. \"price reclaimed EMA9 at 7.10 on rising volume\" or \"MACD histogram crossed "
        "positive at 7.12\"). If there genuinely was no clean signal -- the entry chased an "
        "extended move, the exit was a panic bail with no structural trigger -- say that plainly "
        "instead of inventing one (e.g. \"no fresh trigger -- price was already 8% above VWAP "
        "with no pullback or breakout level touched\"). Always cite a specific number from the "
        "data above, whether the signal was good or absent.\n\n"
        "Respond with JSON only, no markdown: {\"verdict\": \"good entry|late entry|should have "
        "avoided\", \"setup_type\": \"red_to_green|green_over_green_wick|ema9_dip_buy|chase|other\", \"reasoning\": string (3-5 "
        "sentences covering whether the setup criteria were plausibly met), "
        "\"better_entry_price\": number (never null -- always propose one, see instructions "
        "above), \"better_entry_time\": string (never null), \"better_exit_price\": number "
        "(never null -- always propose one, see instructions above), \"better_exit_time\": "
        "string (never null), \"suggested_stop\": number, \"suggested_target\": number, "
        "\"risk_reward\": string (e.g. \"1:2\"), \"walk_away_rule\": string, "
        "\"better_entry_reason\": string (never null -- always cite a specific number, why that "
        "price/time beats the actual entry), \"better_exit_reason\": string (never null -- "
        "always cite a specific number, why that price/time beats the actual exit), "
        "\"better_entry_how_to_know\": string (never null -- the specific real-time signal that "
        "flags the better entry), \"better_exit_how_to_know\": string (never null -- the "
        "specific real-time signal that flags the better exit), \"entry_indicator\": string "
        "(never null -- the specific real-time signal present at the ACTUAL entry price/time, or "
        "an honest note that none was present), \"exit_indicator\": string (never null -- the "
        "specific real-time signal present at the ACTUAL exit price/time, or an honest note that "
        "none was present), \"lessons\": array of EXACTLY 2-3 objects {\"lesson\": string, "
        "\"tag\": string} (no how_to_know field on these -- that belongs only on the "
        "better-entry/exit fields above), NEVER empty even on a clean win (name "
        "something to refine next time -- sizing, hold time, exit timing, stop placement, etc.), "
        "each \"lesson\" a concrete mistake-or-refinement pair from THIS trade citing a specific "
        "number where possible, and each \"tag\" a short snake_case category picked from (or, if truly none "
        "fit, coined in the same style as) this set: chased_extension, late_entry, late_exit, "
        "ignored_volume, no_stop_discipline, sized_too_big, held_through_reversal, "
        "entered_against_trend, exited_too_early, good_execution"
        f"{symbol_info_schema}}}"
    )


_VERDICT_PARSE_ERROR_FALLBACK = {
    "verdict": "llm_parse_error", "setup_type": "", "better_entry_price": None,
    "better_entry_time": None, "better_exit_price": None, "better_exit_time": None,
    "better_entry_reason": None, "better_exit_reason": None,
    "better_entry_how_to_know": None, "better_exit_how_to_know": None,
    "entry_indicator": None, "exit_indicator": None, "suggested_stop": None,
    "suggested_target": None, "risk_reward": "", "walk_away_rule": "", "lessons": [],
    "symbol_name": "", "symbol_country": "", "symbol_sector": "", "symbol_description": "",
}


_BETTER_ENTRY_FIELDS = ("better_entry_price", "better_entry_time", "better_entry_reason", "better_entry_how_to_know")
_BETTER_EXIT_FIELDS = ("better_exit_price", "better_exit_time", "better_exit_reason", "better_exit_how_to_know")


def _parse_bar_timestamp(value):
    """Best-effort parse of a better_entry_time/better_exit_time string
    into a naive datetime for ordering comparisons only. These are
    supposed to be copied verbatim from serialize_bars' "t" field
    (%Y-%m-%dT%H:%M:%S -- see _bar_table_for_prompt), but the model can
    still hand back something slightly off (a space instead of "T", no
    seconds), so this tries a couple of reasonable fallbacks rather than
    just giving up. Returns None if it truly can't be parsed."""
    if not value:
        return None
    text = str(value).strip()
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _enforce_better_entry_before_exit(verdict: dict, trade: dict) -> dict:
    """Gemini is prompted to propose better_entry_time and
    better_exit_time independently (see _build_verdict_prompt), and
    nothing about that prompt stops it from picking a "better entry"
    timestamp that's chronologically AFTER its own "better exit"
    timestamp -- an entry that happens after the trade has already been
    exited is nonsensical, but the model has no structural reason not to
    do it, and _apply_verdict/charts.py just plot whatever comes back.
    That's what shows up on the trade chart as a better-entry marker
    sitting to the right of (or on) the better-exit marker.

    Belt-and-suspenders check here, after parsing and before this
    verdict is used anywhere: if both times are present and parseable
    and entry >= exit, the pair is broken and we can't tell which half
    the model got wrong -- so drop both rather than ship a marker pair
    that contradicts itself on the chart. Logged so bad Gemini responses
    are visible instead of silently discarded.
    """
    entry_time = verdict.get("better_entry_time")
    exit_time = verdict.get("better_exit_time")
    entry_ts = _parse_bar_timestamp(entry_time)
    exit_ts = _parse_bar_timestamp(exit_time)
    if entry_ts is not None and exit_ts is not None and entry_ts >= exit_ts:
        log.warning(
            "Discarding better-entry/exit for %s %s: proposed better_entry_time %r "
            "is not before proposed better_exit_time %r",
            trade.get("Symbol"), trade.get("Trade Date"), entry_time, exit_time,
        )
        for field in _BETTER_ENTRY_FIELDS + _BETTER_EXIT_FIELDS:
            verdict[field] = None
    return verdict


def _normalize_lessons(raw) -> list:
    """Gemini's response_mime_type: application/json guarantees valid JSON
    overall, but not that nested objects STAY objects -- each element of
    "lessons" sometimes comes back as a JSON-encoded string (the same
    {"lesson": ..., "how_to_know": ..., "tag": ...} shape, just
    double-encoded) instead of a real nested object. Undetected, that
    string flows verbatim through _apply_verdict -> publish.py -> Supabase
    and prints as raw JSON text everywhere lessons are displayed. Un-
    stringify any element that needs it here, at the one place every
    lesson passes through right after Gemini returns it, so every
    downstream consumer always sees a real list of dicts.
    """
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, str):
            try:
                parsed = json.loads(item)
            except (TypeError, ValueError):
                parsed = None
            item = parsed if isinstance(parsed, dict) else {"lesson": item}
        if isinstance(item, dict):
            out.append(item)
    return out


def _get_verdict(trade: dict, indicator_summary: str, cached_symbol_info: dict | None = None) -> dict:
    """Calls Gemini and parses the verdict JSON, mirrors "Parse Verdict &
    Attach Chart"'s try/except-to-llm_parse_error behavior exactly.

    cached_symbol_info (see symbol_info_store.py) is this symbol's
    already-known name/country/sector/description, if any. When present,
    the prompt skips asking Gemini for those four fields at all (see
    _build_verdict_prompt's need_symbol_info) and they're merged onto the
    verdict here instead of round-tripping through the model again. When
    absent (first time this symbol's been graded), the same call still
    asks for them as before, and a non-empty answer gets saved to
    symbol_info_store so trade 2 of this symbol skips asking."""
    from ai_routes import _call_gemini, _extract_text  # lazy, same pattern as everywhere else in this service

    need_symbol_info = cached_symbol_info is None
    prompt = _build_verdict_prompt(trade, indicator_summary, need_symbol_info)
    try:
        gem = _call_gemini({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "response_mime_type": "application/json",
                # This call never set thinkingConfig before, which for a
                # Gemini 3-family model means it was running at whatever
                # that model's *default* thinking level is -- documented
                # as the highest tier for Gemini 3 Flash unless told
                # otherwise. "minimal" is the lowest tier available
                # (there's no true "off") and is meant to match "no
                # thinking" for most queries -- worth confirming against
                # real logs after this ships, but the existing
                # try/except above already falls back gracefully to
                # _VERDICT_PARSE_ERROR_FALLBACK if a call ever comes back
                # malformed, so the blast radius of this being wrong is
                # small.
                "thinkingConfig": {"thinkingLevel": "minimal"},
            },
        }, timeout=GEMINI_TIMEOUT_S)
        text = _extract_text(gem)
        verdict = json.loads(text)
        if not isinstance(verdict, dict):
            raise ValueError("verdict JSON was not an object")
    except Exception as e:
        log.warning("Gemini verdict call/parse failed for %s %s: %s", trade.get("Symbol"), trade.get("Trade Date"), e)
        verdict = dict(_VERDICT_PARSE_ERROR_FALLBACK)
        verdict["reasoning"] = f"Could not parse Gemini response: {e}"
    verdict["lessons"] = _normalize_lessons(verdict.get("lessons"))
    verdict = _enforce_better_entry_before_exit(verdict, trade)

    if cached_symbol_info is not None:
        verdict["symbol_name"] = cached_symbol_info.get("name") or ""
        verdict["symbol_country"] = cached_symbol_info.get("country") or ""
        verdict["symbol_sector"] = cached_symbol_info.get("sector") or ""
        verdict["symbol_description"] = cached_symbol_info.get("description") or ""
    elif any(verdict.get(k) for k in ("symbol_name", "symbol_country", "symbol_sector", "symbol_description")):
        try:
            symbol_info_store.save_symbol_info(
                trade.get("Symbol"),
                verdict.get("symbol_name") or "", verdict.get("symbol_country") or "",
                verdict.get("symbol_sector") or "", verdict.get("symbol_description") or "",
            )
        except Exception as e:
            # Non-fatal: worst case this symbol just asks Gemini for its
            # info again on its next trade instead of being served from
            # the table.
            log.warning("symbol_info_store save failed for %s (non-fatal): %s", trade.get("Symbol"), e)

    return verdict


def _apply_verdict(trade: dict, verdict: dict) -> dict:
    """Port of the field-mapping half of "Parse Verdict & Attach Chart"."""
    out = dict(trade)
    out["Verdict"] = verdict.get("verdict")
    out["Setup Type"] = verdict.get("setup_type") or ""
    out["Better Entry"] = (
        f"{verdict['better_entry_price']} @ {verdict.get('better_entry_time')}"
        if verdict.get("better_entry_price") else ""
    )
    out["Better Exit"] = (
        f"{verdict['better_exit_price']} @ {verdict.get('better_exit_time')}"
        if verdict.get("better_exit_price") else ""
    )
    out["Better Entry Price"] = verdict.get("better_entry_price")
    out["Better Entry Time"] = verdict.get("better_entry_time")
    out["Better Exit Price"] = verdict.get("better_exit_price")
    out["Better Exit Time"] = verdict.get("better_exit_time")
    out["Better Entry Reason"] = verdict.get("better_entry_reason") or ""
    out["Better Exit Reason"] = verdict.get("better_exit_reason") or ""
    out["Better Entry How To Know"] = verdict.get("better_entry_how_to_know") or ""
    out["Better Exit How To Know"] = verdict.get("better_exit_how_to_know") or ""
    out["Entry Indicator"] = verdict.get("entry_indicator") or ""
    out["Exit Indicator"] = verdict.get("exit_indicator") or ""
    out["Suggested Stop"] = verdict.get("suggested_stop")
    out["Suggested Target"] = verdict.get("suggested_target")
    out["Risk:Reward"] = verdict.get("risk_reward") or ""
    out["Walk-Away Rule"] = verdict.get("walk_away_rule") or ""
    out["Lessons"] = verdict.get("lessons") if isinstance(verdict.get("lessons"), list) else []
    out["Symbol Name"] = verdict.get("symbol_name") or ""
    out["Symbol Country"] = verdict.get("symbol_country") or ""
    out["Symbol Sector"] = verdict.get("symbol_sector") or ""
    out["Symbol Description"] = verdict.get("symbol_description") or ""
    out["LLM Reasoning"] = verdict.get("reasoning")
    return out


def process_trade(trade: dict) -> dict:
    """One closed trade through: chart -> indicator summary -> Gemini
    verdict -> final chart (redrawn with the verdict's better-entry/exit
    overlay) -> attach. Mirrors "Generate Chart" through "Attach Final
    Chart". Never raises -- a failure at any step degrades gracefully
    (null indicators/bars/image) same as the n8n graph's onError:
    continueRegularOutput on both chart calls."""
    from charts import _build_chart_response

    symbol = trade.get("Symbol")
    trade_date = trade.get("Trade Date")
    side = (trade.get("Side") or "Long").lower()

    chart = None
    chart_err = None
    try:
        chart = _build_chart_response({
            "symbol": symbol, "trade_date": trade_date,
            "entry_time": trade.get("Entry Time"), "exit_time": trade.get("Exit Time"),
            "entry_price": trade.get("Entry Price"), "exit_price": trade.get("Exit Price"),
            "side": side, "include_volume_stats": True,
        }, time.monotonic())
    except Exception as e:
        chart_err = f"{type(e).__name__}: {e}"[:250]
        log.error("Generate Chart failed for %s %s: %s", symbol, trade_date, e)

    summary = _indicator_summary(chart)
    try:
        cached_symbol_info = symbol_info_store.get_symbol_info(symbol)
    except Exception as e:
        # Non-fatal: worst case this trade's verdict call just asks
        # Gemini for the symbol's info again instead of skipping it.
        log.warning("symbol_info_store lookup failed for %s (non-fatal): %s", symbol, e)
        cached_symbol_info = None
    verdict = _get_verdict(trade, summary, cached_symbol_info)
    trade = _apply_verdict(trade, verdict)
    # The setup tag is the trader's own plan, read straight off the candles
    # (setups.py) -- not whatever the LLM guessed from breakout/dip_buy/other.
    rule_setup = ((chart or {}).get("indicators") or {}).get("setup_type")
    if rule_setup:
        trade["Setup Type"] = rule_setup

    final_chart = None
    final_err = None
    try:
        final_chart = _build_chart_response({
            "symbol": symbol, "trade_date": trade_date,
            "entry_time": trade.get("Entry Time"), "exit_time": trade.get("Exit Time"),
            "entry_price": trade.get("Entry Price"), "exit_price": trade.get("Exit Price"),
            "side": side,
            "better_entry_price": trade.get("Better Entry Price"),
            "better_entry_time": trade.get("Better Entry Time"),
            "better_exit_price": trade.get("Better Exit Price"),
            "better_exit_time": trade.get("Better Exit Time"),
            "suggested_stop": trade.get("Suggested Stop"),
            "suggested_target": trade.get("Suggested Target"),
            "include_volume_stats": False,
        }, time.monotonic())
    except Exception as e:
        final_err = f"{type(e).__name__}: {e}"[:250]
        log.error("Generate Final Chart failed for %s %s: %s", symbol, trade_date, e)

    # Surface WHY a chart is missing (shown on the import page + in publish
    # results) instead of the trade silently turning into a bare "skipped".
    trade["_chart_error"] = None if final_chart else (final_err or chart_err)

    trade["_final_indicators"] = final_chart["indicators"] if final_chart else None
    trade["_final_bars"] = final_chart["bars"] if final_chart else None
    trade["_final_image_base64"] = (final_chart or {}).get("image_base64") if final_chart else (chart or {}).get("image_base64")
    return trade


# ---------------------------------------------------------------------------
# Per-account + whole-run orchestration
# ---------------------------------------------------------------------------

class SkipAccount(Exception):
    """Not an error: this account is deliberately not published (e.g. wrong IBKR login)."""


def _note_failure(attempts: dict, trade: dict, reason: str | None = None):
    e = attempts.get(trade_id_of(trade)) or {"n": 0, "last": None}
    attempts[trade_id_of(trade)] = {
        "n": int(e.get("n", 0)) + 1, "last": datetime.utcnow().isoformat() + "Z",
        "err": (reason or e.get("err") or "")[:250],
    }


def _stuck_entry(trade: dict, entry: dict | None) -> dict:
    return {
        "symbol": trade.get("Symbol"), "date": trade.get("Trade Date"), "id": trade_id_of(trade),
        "reason": (entry or {}).get("err") or "chart data unavailable for this symbol/date",
        "attempts": int((entry or {}).get("n", 0)), "next_retry": next_retry_of(entry),
    }


def _csv_rows_with_datetime(rows: list[dict]) -> list[dict]:
    """IBKR CSVs may have separate TradeDate / TradeTime columns instead of one DateTime."""
    out = []
    for r in rows:
        low = {str(k).strip().lower().replace("_", "").replace(" ", ""): k for k in r}
        if "datetime" not in low and "tradedate" in low and "tradetime" in low:
            r = {**r, "DateTime": f"{r[low['tradedate']]};{r[low['tradetime']]}"}
        out.append(r)
    return out


def executions_from_statement(flex_doc: dict):
    """(raw_executions, fees, account_ids) from either a Flex XML or a Flex CSV statement."""
    if "FlexCsv" in flex_doc:
        trade_rows, fee_rows = read_csv_sections(flex_doc["FlexCsv"])
        trade_rows = _csv_rows_with_datetime(trade_rows)
        ids = set()
        for r in trade_rows:
            for k, v in r.items():
                if str(k).strip().lower() == "clientaccountid" and v:
                    ids.add(str(v).strip().upper())
        return parse_csv_executions(trade_rows), parse_csv_fees(fee_rows), ids
    stmts = ((flex_doc.get("FlexQueryResponse") or {}).get("FlexStatements") or {}).get("FlexStatement") or []
    stmts = stmts if isinstance(stmts, list) else [stmts]
    ids = {str(st.get("accountId") or "").upper() for st in stmts if isinstance(st, dict)} - {""}
    return parse_flex_executions(flex_doc), parse_flex_fees(flex_doc), ids


def process_account(account: dict) -> list[dict]:
    """Flex request -> parse -> FIFO match -> per-trade chart/verdict, for
    one broker account. Returns the list of fully-enriched closed trades
    (backtest-import never reaches this path, so no _import_source filter
    is needed here)."""
    flex_doc = request_flex_statement(account)
    raw_executions, fees, stmt_ids = executions_from_statement(flex_doc)
    ignored: list = []
    open_positions: list = []
    closed_trades = fifo_match_and_merge(raw_executions, account=account, skipped=ignored, fees=fees,
                                         open_positions=open_positions)
    account["_ignored"], account["_open"], account["_stuck"] = ignored, open_positions, []
    account["_statement"] = statement_info(flex_doc, raw_executions, len(closed_trades))

    # The statement's own account must match the one the user said this token belongs to.
    ref = (account.get("ibkr_account_ref") or "").strip().upper()
    if ref and stmt_ids and ref not in stmt_ids:
        raise SkipAccount(f"statement is for {', '.join(sorted(stmt_ids))}, not {ref}")

    log.info("account %s: %d executions -> %d closed trades", account.get("id"), len(raw_executions), len(closed_trades))
    # Skip trades already saved: no chart, no Polygon, no Gemini for them. This is what makes an
    # hourly run cheap -- a run with nothing new costs one IBKR request.
    try:
        have = existing_trade_ids(account.get("user_id"))
    except requests.RequestException as e:
        log.warning("account %s: could not read existing trades (%s) -- processing everything", account.get("id"), e)
        have = set()
    fresh = [t for t in closed_trades if trade_id_of(t) not in have]
    log.info("account %s: %d already saved, %d new", account.get("id"), len(closed_trades) - len(fresh), len(fresh))

    # Cost guards: (1) a trade that keeps failing to chart is retried at most once per
    # RETRY_COOLDOWN_HOURS after FAST_RETRIES failures (it is never saved without a chart);
    # (2) at most MAX_NEW_TRADES_PER_RUN get the full treatment per run.
    attempts = load_attempts(account.get("user_id"))
    attempts_before = json.loads(json.dumps(attempts))
    waiting = [t for t in fresh if not retry_due(attempts.get(trade_id_of(t)))]
    todo = [t for t in fresh if t not in waiting]
    account["_deferred"] = max(0, len(todo) - MAX_NEW_TRADES_PER_RUN)
    account["_cooling"] = [t.get("Symbol") for t in waiting]
    account["_stuck"].extend(_stuck_entry(t, attempts.get(trade_id_of(t))) for t in waiting)
    closed_trades = todo[:MAX_NEW_TRADES_PER_RUN]
    if account["_deferred"]:
        log.info("account %s: %d more new trades wait for the next run (cap %d)", account.get("id"), account["_deferred"], MAX_NEW_TRADES_PER_RUN)
    for ig in ignored:
        log.info("account %s: ignored %s %s: %s", account.get("id"), ig.get("symbol"), ig.get("date"), ig.get("reason"))

    enriched = []
    for i, trade in enumerate(closed_trades):
        if i > 0:
            time.sleep(CHART_PACING_SECONDS)
        try:
            done = process_trade(trade)
            if not (done.get("_final_indicators") and done.get("_final_bars")):
                _note_failure(attempts, trade, done.get("_chart_error"))
                account.setdefault("_failed", []).append(trade.get("Symbol"))
                account["_stuck"].append(_stuck_entry(trade, attempts.get(trade_id_of(trade))))
            else:
                attempts.pop(trade_id_of(trade), None)   # charted fine: forget earlier failures
            enriched.append(done)
        except Exception as e:
            _note_failure(attempts, trade, f"{type(e).__name__}: {e}")
            account.setdefault("_failed", []).append(trade.get("Symbol"))
            account["_stuck"].append(_stuck_entry(trade, attempts.get(trade_id_of(trade))))
            # process_trade already degrades gracefully internally; this is
            # a last-resort catch so one bad trade can't drop the rest of
            # the account's run.
            log.error("process_trade crashed for %s %s: %s", trade.get("Symbol"), trade.get("Trade Date"), e)
            trade["_final_indicators"] = None
            trade["_final_bars"] = None
            trade["_final_image_base64"] = None
            enriched.append(trade)
    attempts = {k: n for k, n in attempts.items() if k not in have}   # forget trades that are saved now
    if attempts != attempts_before:
        save_attempts(account.get("user_id"), attempts)
    return enriched


def run_daily_sync(accounts: list | None = None) -> dict:
    """Entry point -- one broker account at a time (matches "Loop Broker
    Accounts"'s batchSize: 1). Each account's enriched trades are then run
    through publish.py's Supabase publish step (mirrors "Combine All
    Results" happening once per account/batch in n8n). Returns
    {accounts: [{account_id, trades: [...], publish_results: [...],
    error: str|null}, ...]}."""
    if accounts is None:
        accounts = get_broker_accounts()
    results = []
    for n, account in enumerate(accounts):
        if n > 0:
            time.sleep(ACCOUNT_PAUSE_SECONDS)  # IBKR throttles Flex requests per token
        try:
            trades = process_account(account)
        except SkipAccount as e:
            log.warning("account %s skipped: %s", account.get("id"), e)
            report_status(account, "skipped", str(e))
            results.append({"account_id": account.get("id"), "trades": [], "publish_results": [], "error": None, "skipped": str(e)})
            continue
        except Exception as e:
            log.error("account %s failed entirely: %s", account.get("id"), e)
            report_status(account, "failed", str(e))
            results.append({"account_id": account.get("id"), "trades": [], "publish_results": [], "error": str(e)})
            continue

        publish_results = []
        try:
            publish_results = publish_trades(trades)
            published = sum(1 for r in publish_results if r["status"] == "published")
            failed = sum(1 for r in publish_results if r["status"] == "failed")
            skipped = sum(1 for r in publish_results if r["status"] == "skipped")
            log.info(
                "account %s: publish done -- %d published, %d failed, %d skipped",
                account.get("id"), published, failed, skipped,
            )
            if failed:
                report_status(account, "failed", f"{published} saved, {failed} failed to save")
            else:
                msg = f"{published} new trade(s)" if published else "No new trades"
                sti = account.get("_statement") or {}
                if not published and sti:
                    msg += (f" (IBKR sent {sti.get('fills', 0)} fill(s), latest {sti['latest_fill']})" if sti.get("fills")
                            else " (IBKR's statement had no fills" + (f" for {sti['from']} to {sti['to']}" if sti.get("from") else "") + ")")
                if account.get("_deferred"):
                    msg += f", {account['_deferred']} more waiting for the next run"
                stuck = (account.get("_failed") or []) + (account.get("_cooling") or [])
                if stuck:
                    msg += f", no chart yet for {', '.join(sorted(set(map(str, stuck))))} (will retry, not saved without it)"
                report_status(account, "ok", msg)
            save_account_report(account, build_report(account, publish_results))
        except Exception as e:
            log.error("account %s: publish step failed entirely: %s", account.get("id"), e)
            report_status(account, "failed", f"saving failed: {e}")

        results.append({"account_id": account.get("id"), "trades": trades, "publish_results": publish_results, "error": None})
    return {"accounts": results, "run_at": datetime.utcnow().isoformat() + "Z"}


def _run_and_save(job_id: str):
    result = run_daily_sync()
    DAILY_SYNC_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DAILY_SYNC_RUNS_DIR / f"{job_id}.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    total_trades = sum(len(a["trades"]) for a in result["accounts"])
    log.info("daily-sync job %s: done, %d accounts, %d trades -> %s", job_id, len(result["accounts"]), total_trades, out_path)


# Shared secret for /daily-sync. The endpoint runs the whole pipeline for EVERY
# user (their IBKR Flex tokens, Polygon, the LLM) and the service URL is public
# (it's in the site's config.js), so it must not be triggerable by anyone who
# knows the URL. Set DAILY_SYNC_SECRET in Render and the same value as a GitHub
# Actions secret; the workflow sends it as the X-Sync-Token header.
# If the env var isn't set yet the endpoint stays open (so an existing
# deployment doesn't silently stop syncing) but logs a warning on every call.
DAILY_SYNC_SECRET = os.environ.get("DAILY_SYNC_SECRET", "")
_daily_sync_lock = threading.Lock()


@bp.route("/daily-sync", methods=["POST"])
def daily_sync():
    """Fires the whole pipeline in a background thread and returns
    immediately -- this can run long (broker-account count x trade count x
    ~13s chart pacing x Gemini call latency), well past what Render's
    request proxy or a GitHub Actions HTTP step would wait for. Inspect
    DAILY_SYNC_RUNS_DIR/<job_id>.json once it's done; part 2 will replace
    that file-write with the real Sheets/Telegram/Supabase publish.

    Guarded by DAILY_SYNC_SECRET (X-Sync-Token header) and allows only one
    run at a time -- see the comment above DAILY_SYNC_SECRET."""
    import hmac
    from flask import request
    if DAILY_SYNC_SECRET:
        supplied = request.headers.get("X-Sync-Token", "")
        if not hmac.compare_digest(supplied.encode(), DAILY_SYNC_SECRET.encode()):
            log.warning("daily-sync: rejected request with missing/invalid X-Sync-Token")
            return jsonify({"error": "unauthorized"}), 401
    else:
        log.warning("daily-sync: DAILY_SYNC_SECRET is not set -- endpoint is UNPROTECTED")

    # One run at a time: overlapping runs double-hit IBKR Flex (which rate-limits
    # and can invalidate a token) and race on the equity_after recompute.
    if not _daily_sync_lock.acquire(blocking=False):
        return jsonify({"error": "a daily sync is already running"}), 409

    job_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    def _run_then_release(jid):
        try:
            _run_and_save(jid)
        finally:
            _daily_sync_lock.release()

    try:
        th = threading.Thread(target=_run_then_release, args=(job_id,), daemon=True)
        th.start()
    except Exception:
        _daily_sync_lock.release()
        raise
    log.info("daily-sync job %s: started", job_id)
    return jsonify({"started": job_id}), 202



# ---------------------------------------------------------------------------
# "Sync now" button on the Accounts page: one account, the logged-in user's own.
# Auth is the user's Supabase token (like /import-trades), not the cron secret.
# It shares the cron's lock (never two runs at once -- IBKR rate-limits Flex) and has a
# short per-account cooldown so a double-click or an impatient tap can't hammer IBKR.
# ---------------------------------------------------------------------------
MANUAL_SYNC_COOLDOWN_S = float(os.environ.get("SYNC_NOW_COOLDOWN_SECONDS", "90"))
_manual_last: dict[str, float] = {}


@bp.route("/sync-now", methods=["POST", "OPTIONS"])
def sync_now():
    from flask import request
    from supabase_auth import resolve_user_id
    if request.method == "OPTIONS":
        return "", 204
    user_id = resolve_user_id(request.headers.get("Authorization"))
    if not user_id:
        return jsonify({"error": "missing or invalid Authorization token -- please log in and try again"}), 401
    account_id = str((request.get_json(silent=True) or {}).get("account_id") or "").strip()
    if not account_id:
        return jsonify({"error": "account_id is required"}), 400

    try:
        mine = [a for a in get_broker_accounts()
                if a.get("user_id") == user_id and account_id in (str(a.get("id")), str(a.get("account_id")))]
    except Exception as e:
        log.error("sync-now: could not load accounts: %s", e)
        return jsonify({"error": "could not read your broker connection -- try again in a minute"}), 503
    if not mine:
        return jsonify({"error": "this account has no connected Flex token and query yet"}), 404

    wait = MANUAL_SYNC_COOLDOWN_S - (time.time() - _manual_last.get(account_id, 0))
    if wait > 0:
        return jsonify({"error": f"just synced -- wait {int(wait) + 1}s before pulling again"}), 429
    if not _daily_sync_lock.acquire(blocking=False):
        return jsonify({"error": "a sync is already running -- give it a few minutes, then check the status here"}), 409
    _manual_last[account_id] = time.time()

    def _run(accts):
        try:
            run_daily_sync(accounts=accts)
        except Exception as e:   # run_daily_sync reports per-account failures itself; this is a last resort
            log.error("sync-now for %s crashed: %s", account_id, e)
        finally:
            _daily_sync_lock.release()

    try:
        threading.Thread(target=_run, args=(mine,), daemon=True).start()
    except Exception:
        _daily_sync_lock.release()
        raise
    log.info("sync-now: started for account %s (user %s)", account_id, user_id)
    return jsonify({"started": True}), 202

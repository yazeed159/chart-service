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

from flex_xml import parse_flex_xml
from trade_matching import fifo_match_and_merge, parse_flex_executions
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

def get_broker_accounts() -> list[dict]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set")
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/broker_accounts",
        params={"select": "*"},
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Flex request + wait/retry poll
# ---------------------------------------------------------------------------

def _send_flex_request(token: str, query_id: str) -> dict:
    resp = requests.get(
        FLEX_SEND_URL,
        params={"t": token, "q": query_id, "v": "3"},
        headers={"User-Agent": FLEX_USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    return parse_flex_xml(resp.content)


def _get_statement(token: str, reference_code: str) -> dict:
    resp = requests.get(
        FLEX_GET_URL,
        params={"t": token, "q": reference_code, "v": "3"},
        headers={"User-Agent": FLEX_USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    return parse_flex_xml(resp.content)


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
        if stmt_doc.get("FlexQueryResponse"):
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
        f"- MACD: {fmt(indicators.get('macd_at_entry'), 4)}, Signal: {fmt(indicators.get('macd_signal_at_entry'), 4)}, "
        f"Histogram: {fmt(indicators.get('macd_hist_at_entry'), 4)} (prior bar: {fmt(indicators.get('macd_hist_prior_bar'), 4)})",
        f"- Service-computed setup_type guess: {indicators.get('setup_type', 'n/a')}",
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
        "hindsight description. Do the same for each lesson: pair it with a how_to_know "
        "describing the specific, checkable signal that would have told the trader to act "
        "differently in the moment.\n\n"
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
        "avoided\", \"setup_type\": \"breakout|dip_buy|other\", \"reasoning\": string (3-5 "
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
        "\"how_to_know\": string, \"tag\": string}, NEVER empty even on a clean win (name "
        "something to refine next time -- sizing, hold time, exit timing, stop placement, etc.), "
        "each \"lesson\" a concrete mistake-or-refinement pair from THIS trade citing a specific "
        "number where possible, each \"how_to_know\" the specific, checkable real-time signal for "
        "that lesson, and each \"tag\" a short snake_case category picked from (or, if truly none "
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
    from chart_service import _build_chart_response

    symbol = trade.get("Symbol")
    trade_date = trade.get("Trade Date")
    side = (trade.get("Side") or "Long").lower()

    chart = None
    try:
        chart = _build_chart_response({
            "symbol": symbol, "trade_date": trade_date,
            "entry_time": trade.get("Entry Time"), "exit_time": trade.get("Exit Time"),
            "entry_price": trade.get("Entry Price"), "exit_price": trade.get("Exit Price"),
            "side": side, "include_volume_stats": True,
        }, time.monotonic())
    except Exception as e:
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

    final_chart = None
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
        log.error("Generate Final Chart failed for %s %s: %s", symbol, trade_date, e)

    trade["_final_indicators"] = final_chart["indicators"] if final_chart else None
    trade["_final_bars"] = final_chart["bars"] if final_chart else None
    trade["_final_image_base64"] = (final_chart or {}).get("image_base64") if final_chart else (chart or {}).get("image_base64")
    return trade


# ---------------------------------------------------------------------------
# Per-account + whole-run orchestration
# ---------------------------------------------------------------------------

def process_account(account: dict) -> list[dict]:
    """Flex request -> parse -> FIFO match -> per-trade chart/verdict, for
    one broker account. Returns the list of fully-enriched closed trades
    (backtest-import never reaches this path, so no _import_source filter
    is needed here)."""
    flex_doc = request_flex_statement(account)
    raw_executions = parse_flex_executions(flex_doc)
    closed_trades = fifo_match_and_merge(raw_executions, account=account)

    log.info("account %s: %d executions -> %d closed trades", account.get("id"), len(raw_executions), len(closed_trades))

    enriched = []
    for i, trade in enumerate(closed_trades):
        if i > 0:
            time.sleep(CHART_PACING_SECONDS)
        try:
            enriched.append(process_trade(trade))
        except Exception as e:
            # process_trade already degrades gracefully internally; this is
            # a last-resort catch so one bad trade can't drop the rest of
            # the account's run.
            log.error("process_trade crashed for %s %s: %s", trade.get("Symbol"), trade.get("Trade Date"), e)
            trade["_final_indicators"] = None
            trade["_final_bars"] = None
            trade["_final_image_base64"] = None
            enriched.append(trade)
    return enriched


def run_daily_sync() -> dict:
    """Entry point -- one broker account at a time (matches "Loop Broker
    Accounts"'s batchSize: 1). Each account's enriched trades are then run
    through publish.py's Supabase publish step (mirrors "Combine All
    Results" happening once per account/batch in n8n). Returns
    {accounts: [{account_id, trades: [...], publish_results: [...],
    error: str|null}, ...]}."""
    accounts = get_broker_accounts()
    results = []
    for account in accounts:
        try:
            trades = process_account(account)
        except Exception as e:
            log.error("account %s failed entirely: %s", account.get("id"), e)
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
        except Exception as e:
            log.error("account %s: publish step failed entirely: %s", account.get("id"), e)

        results.append({"account_id": account.get("id"), "trades": trades, "publish_results": publish_results, "error": None})
    return {"accounts": results, "run_at": datetime.utcnow().isoformat() + "Z"}


def _run_and_save(job_id: str):
    result = run_daily_sync()
    DAILY_SYNC_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DAILY_SYNC_RUNS_DIR / f"{job_id}.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    total_trades = sum(len(a["trades"]) for a in result["accounts"])
    log.info("daily-sync job %s: done, %d accounts, %d trades -> %s", job_id, len(result["accounts"]), total_trades, out_path)


@bp.route("/daily-sync", methods=["POST"])
def daily_sync():
    """Fires the whole pipeline in a background thread and returns
    immediately -- this can run long (broker-account count x trade count x
    ~13s chart pacing x Gemini call latency), well past what Render's
    request proxy or a GitHub Actions HTTP step would wait for. Inspect
    DAILY_SYNC_RUNS_DIR/<job_id>.json once it's done; part 2 will replace
    that file-write with the real Sheets/Telegram/Supabase publish."""
    job_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    th = threading.Thread(target=_run_and_save, args=(job_id,), daemon=True)
    th.start()
    log.info("daily-sync job %s: started", job_id)
    return jsonify({"started": job_id}), 202

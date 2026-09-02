"""
ai_routes.py
Flask Blueprint that replaces three of the n8n webhook branches -- the ones
your site (dashboard/*.js) calls directly and that n8n only ever forwarded
to Gemini and back. These are the "easy" migration piece: each one is
already stateless request-in/JSON-out, same as chart_service.py's own
/generate-chart. Register it from chart_service.py once app/Flask exists:

    from ai_routes import bp as ai_bp
    app.register_blueprint(ai_bp)

(add that import at the BOTTOM of chart_service.py, after everything else
is defined -- these routes import from chart_service lazily inside each
function body, not at module load time, specifically so it doesn't matter
which file gets imported first.)

Routes (same request/response contracts as the n8n webhooks they replace):

POST /support-resistance
  { "symbol": "AAPL", "trade_date": "2026-08-12", "lookback_days": 40 }
  -> { "symbol", "trade_date", "support": [...], "resistance": [...],
       "summary", "source": "llm" | "computed_fallback" }
  Reuses chart_service.py's own _build_daily_chart_response() for the bars
  + pivot levels (no duplicate Polygon-fetching logic), then asks Gemini to
  refine/confirm them. Falls back to the computed pivot levels if Gemini's
  reply doesn't parse.

POST /trade-chat
  { "message", "history": [{role, content}, ...], "trades_summary": {...},
    "trades_sample": [...], "matched_trade_line": str|null,
    "chart_context": {...}|null }
  -> { "reply": str }

POST /backtest-ai
  { "message", "history": [...], "draft": {...}, "schema": {...} }
  -> { "reply", "config": {...}, "unsupported": [...], "status": "asking"|"ready" }

NOT included here: POST /backtest-import. That one is entangled with the
daily-sync pipeline's own trade-matching/extraction code (Extract & Match
Trades1, Prepare Trade Payloads in the n8n workflow) which hasn't been
ported yet -- it belongs with that piece, not this one.

Env vars (new, on top of chart_service.py's existing ones):
  GEMINI_API_KEY  - same key the n8n googlePalmApi credential held
  GEMINI_MODEL    - defaults to gemini-3.5-flash-lite, matching the n8n
                     workflow's HTTP Request node URLs
"""

import os
import re
import json
import time
import logging

import requests
from flask import Blueprint, request, jsonify

log = logging.getLogger("chart_service.ai_routes")

bp = Blueprint("ai_routes", __name__)

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# The n8n workflow set 45s node timeouts on SR/Chat and 180s on Backtest AI
# (its prompt runs largest + this model got slower as it grew -- see that
# node's own comments). Match those here rather than one shared value.
SR_TIMEOUT_S = 45
CHAT_TIMEOUT_S = 45
BACKTEST_AI_TIMEOUT_S = 175


def _call_gemini(payload: dict, timeout: float) -> dict:
    resp = requests.post(GEMINI_URL, params={"key": GEMINI_API_KEY}, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _extract_text(gemini_response: dict) -> str:
    try:
        parts = gemini_response["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# POST /support-resistance
# ---------------------------------------------------------------------------

@bp.route("/support-resistance", methods=["POST", "OPTIONS"])
def support_resistance():
    if request.method == "OPTIONS":
        return "", 204

    from chart_service import _build_daily_chart_response, SR_LOOKBACK_DAYS_DEFAULT

    start = time.monotonic()
    body = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    trade_date = body.get("trade_date")
    if not symbol or not trade_date:
        return jsonify({"error": "symbol and trade_date are required"}), 400

    try:
        daily = _build_daily_chart_response({
            "symbol": symbol,
            "trade_date": trade_date,
            "lookback_days": body.get("lookback_days") or SR_LOOKBACK_DAYS_DEFAULT,
        })
    except Exception as e:
        log.error("SR: daily chart build failed for %s: %s", symbol, e)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    bars = daily.get("bars") or []
    fallback = daily.get("computed_levels") or {"support": [], "resistance": []}

    def _lv_line(levels):
        return ", ".join(f"${x['price']} ({x['touches']}x)" for x in levels) or "none found"

    lines = [f"{b['t']}: O{b['o']} H{b['h']} L{b['l']} C{b['c']} V{b['v']}" for b in bars]
    daily_summary = "\n".join([
        f"Daily bars for {symbol}, most recent {len(bars)} trading days BEFORE {trade_date} (oldest first):",
        *lines,
        "",
        f"Computer-detected pivot support: {_lv_line(fallback.get('support', []))}",
        f"Computer-detected pivot resistance: {_lv_line(fallback.get('resistance', []))}",
    ])

    prompt_text = (
        "You are a day-trading technical analyst reading a daily candlestick chart. Below are the "
        "last several days of daily OHLCV bars for a symbol, ending the trading day BEFORE an "
        "actual trade was taken (so only information the trader could have seen going into the "
        "trade), plus a computer-detected pivot-cluster list you should sanity-check and refine "
        "rather than ignore.\n\n"
        f"{daily_summary}\n\n"
        "Identify up to 4 key SUPPORT levels (prices below the most recent close where price has "
        "repeatedly held or bounced) and up to 4 key RESISTANCE levels (prices above the most "
        "recent close where price has repeatedly stalled or reversed). Prefer levels with multiple "
        "touches over single-touch spikes. Give a one-sentence summary of the overall daily "
        "structure (trend, and which nearby level matters most).\n\n"
        'Respond with JSON only, no markdown: {"support": [{"price": number, "label": string '
        '(why this level matters, one short phrase)}], "resistance": [{"price": number, "label": '
        'string}], "summary": string}'
    )

    parsed = None
    try:
        gem = _call_gemini({
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": {"response_mime_type": "application/json"},
        }, timeout=SR_TIMEOUT_S)
        text = _extract_text(gem)
        parsed = json.loads(text) if text else None
    except (requests.RequestException, json.JSONDecodeError, TypeError) as e:
        log.warning("SR: Gemini call/parse failed for %s after %.1fs: %s", symbol, time.monotonic() - start, e)
        parsed = None

    if not isinstance(parsed, dict):
        return jsonify({
            "symbol": symbol, "trade_date": trade_date,
            "support": fallback.get("support", []),
            "resistance": fallback.get("resistance", []),
            "summary": "LLM response could not be parsed -- showing computer-detected pivot levels instead.",
            "source": "computed_fallback",
        })

    support = parsed.get("support") or fallback.get("support", [])
    resistance = parsed.get("resistance") or fallback.get("resistance", [])
    log.info("SR done for %s in %.1fs", symbol, time.monotonic() - start)
    return jsonify({
        "symbol": symbol, "trade_date": trade_date,
        "support": support, "resistance": resistance,
        "summary": parsed.get("summary", ""),
        "source": "llm",
    })


# ---------------------------------------------------------------------------
# POST /trade-chat
# ---------------------------------------------------------------------------

@bp.route("/trade-chat", methods=["POST", "OPTIONS"])
def trade_chat():
    if request.method == "OPTIONS":
        return "", 204

    body = request.get_json(force=True, silent=True) or {}
    message = str(body.get("message") or "")[:4000]
    history = body.get("history") if isinstance(body.get("history"), list) else []
    summary = body.get("trades_summary") or {}
    sample = body.get("trades_sample") if isinstance(body.get("trades_sample"), list) else []
    matched_trade_line = body.get("matched_trade_line")
    chart_context = body.get("chart_context")

    system_parts = [
        "You are the AI assistant embedded in this trader's personal trading journal (trade.log).",
        "The trader runs a Ross Cameron style small-cap breakout and momentum strategy: low-float,",
        "high-relative-volume stocks in play on news or gaps, entries on VWAP reclaims, opening-range",
        "breakouts, first-pullback / first-green-day setups, bull flags and micro pullbacks inside an",
        "established momentum move, scaling out into strength, and cutting losers fast at a hard stop",
        "instead of averaging down or hoping. Chasing extended moves far above VWAP, moving a stop",
        "further away, oversized position risk, and holding once the setup is clearly invalidated are",
        "all deviations from that playbook -- call them out plainly when the data shows them, citing",
        "the actual trade.",
        "",
        "Aggregate stats across ALL of the trader's logged trades (computed client-side from",
        "data/trades.json -- this covers win rate, PnL, per-setup breakdown, and lesson-tag frequency",
        "for the full journal, not just the sample below):",
        json.dumps(summary, indent=2),
        "",
        f"Curated sample ({len(sample)} rows, NOT the full journal -- this is the worst losses, best",
        "wins, and most recent trades, deduped, oldest first, one line each --",
        "id | date | symbol | side | setup_type | result | net_pnl | shares | entry->exit | lesson_tags | rvol_tag):",
        "\n".join(sample),
    ]
    if matched_trade_line:
        system_parts += [
            "",
            "The trader's message appears to reference this specific logged trade (same line format as above):",
            matched_trade_line,
        ]
    if chart_context and chart_context.get("indicators"):
        system_parts += [
            "",
            f"Live chart indicators for {chart_context.get('symbol')} on {chart_context.get('trade_date')} "
            "(fetched just now from Polygon via chart_service.py, price/VWAP/EMA/MACD at entry, computed "
            "S/R stop & target, volume/float context):",
            json.dumps(chart_context["indicators"], indent=2),
        ]
    system_parts += [
        "",
        "Only state facts supported by the data above. The aggregate stats cover the full journal; the",
        "sample and any matched trade are illustrative, not exhaustive. If asked about a specific trade,",
        "date, or number that isn't in the data above, say plainly that you don't have that detail rather",
        "than inventing it -- don't assume a trade is representative of the whole journal, or vice versa.",
        "Reference specific trades/dates/numbers where it helps. Keep answers conversational and only",
        "as long as the question calls for.",
    ]
    system_prompt = "\n".join(system_parts)

    contents = []
    for turn in history[-8:]:
        role = "model" if turn.get("role") == "assistant" else "user"
        text = str(turn.get("content") or "")[:2000]
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    reply = ""
    try:
        gem = _call_gemini({
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {
                "temperature": 0.4,
                "maxOutputTokens": 2048,
                "thinkingConfig": {"thinkingLevel": "low"},
            },
        }, timeout=CHAT_TIMEOUT_S)
        reply = _extract_text(gem)
    except requests.RequestException as e:
        log.error("Chat: Gemini call failed: %s", e)

    if not reply:
        reply = "Sorry, I couldn't generate a response just now -- give it another try."
    return jsonify({"reply": reply})


# ---------------------------------------------------------------------------
# POST /backtest-ai
# ---------------------------------------------------------------------------

@bp.route("/backtest-ai", methods=["POST", "OPTIONS"])
def backtest_ai():
    if request.method == "OPTIONS":
        return "", 204

    body = request.get_json(force=True, silent=True) or {}
    message = str(body.get("message") or "")[:4000]
    history = body.get("history") if isinstance(body.get("history"), list) else []
    draft = body.get("draft") or {}
    schema = body.get("schema") or {}

    schema_lines = []
    for key, spec in schema.items():
        spec = spec or {}
        type_ = spec.get("type", "string")
        label = spec.get("label", key)
        values = ""
        if type_ == "enum" and isinstance(spec.get("values"), list):
            values = f" (one of: {', '.join(spec['values'])})"
        schema_lines.append(f"- {key} [{type_}]{values} -- {label}")

    system_parts = [
        "You are helping a trader configure a backtest for a Ross Cameron style small-cap",
        "breakout/momentum strategy through a short back-and-forth conversation. Your job each turn is",
        "to (a) pull out any field values the trader's latest message resolves, (b) ask about the single",
        "most important field still missing -- one question at a time, never a checklist -- and (c) once",
        "the fields that matter for a sane backtest are resolved, say so and stop asking.",
        "",
        "Only use these exact field names -- never invent a field name or an enum value not listed here:",
        "\n".join(schema_lines),
        "",
        "Not every field needs a value -- entry_mode-specific fields (e.g. orb_minutes only matters for",
        "orb_breakout, atr_period/atr_mult only for atr_multiple stops) only apply when the relevant mode",
        "is chosen. A reasonable minimum before marking this ready: start, end, entry_mode, stop_mode,",
        "and position_size resolved, plus whichever mode-specific fields that entry/stop choice implies.",
        "Everything else can default silently rather than being asked about.",
        "",
        "IMPORTANT -- the backtest engine is a fixed simulator. It can ONLY act on the fields listed",
        "above; it has no concept of anything else, no matter how reasonable it sounds (examples: float",
        "or share-count filters, relative volume thresholds, limiting to the first N runners of the day,",
        "multi-candle confirmation patterns, EMA/VWAP conditions beyond what stop_mode/entry_mode already",
        "cover, scaling out of a position, discretionary judgment calls). When the trader describes",
        "something like that:",
        "  1. Do NOT invent a new field name and do NOT quietly force it into the closest-sounding",
        "     existing field (e.g. do not fold a float filter into min_dollar_volume) -- that would make",
        "     the backtest silently simulate something other than what they asked for.",
        "  2. Put their own words for it, close to verbatim, into the `unsupported` array described",
        "     below, and separately fold a short semicolon-joined summary of everything in that array",
        "     into `config.notes` (a plain string field -- append to any notes already in the draft above",
        "     rather than replacing them) so it's saved with the run even though it won't be simulated.",
        "  3. Say so plainly in `reply` too (briefly -- one sentence is enough) so the trader isn't left",
        "     thinking it was applied when it wasn't. Still ask about / resolve whatever fields you can.",
        "",
        "Fields already resolved from earlier turns in this conversation (the running draft):",
        json.dumps(draft, indent=2),
        "",
        "Respond with ONLY a raw JSON object -- no markdown code fences, no backticks, no text before or "
        "after it -- shaped exactly like:",
        '{ "reply": "<conversational text -- a follow-up question, or a short wrap-up line>",',
        '  "config": { <any field:value pairs this turn resolved, using the exact field names above> },',
        '  "unsupported": [ "<verbatim/close-paraphrase of anything the trader asked for with no matching '
        'field above>", ... ],',
        '  "status": "asking" | "ready" }',
        "Coerce values to the type shown in brackets (int/float as numbers, bool as true/false, date as",
        "YYYY-MM-DD, time as HH:MM 24h ET). Leave config as {} if nothing new was resolved this turn.",
        "Leave unsupported as [] when everything the trader mentioned this turn has a matching field.",
    ]
    system_prompt = "\n".join(system_parts)

    contents = []
    for turn in history[-8:]:
        role = "model" if turn.get("role") == "assistant" else "user"
        text = str(turn.get("content") or "")[:2000]
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    parsed = None
    try:
        gem = _call_gemini({
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 1024,
                "thinkingConfig": {"thinkingLevel": "low"},
            },
        }, timeout=BACKTEST_AI_TIMEOUT_S)
        raw = _extract_text(gem)
        # No forced JSON mode on this call (times out against this model when
        # combined with response_mime_type -- same reason the n8n node
        # skipped it), so the model may still wrap its answer in ```json
        # fences despite being told not to -- strip those before parsing.
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"```\s*$", "", raw, flags=re.I).strip()
        parsed = json.loads(raw) if raw else None
    except (requests.RequestException, json.JSONDecodeError, TypeError) as e:
        log.warning("Backtest AI: Gemini call/parse failed: %s", e)
        parsed = None

    if not isinstance(parsed, dict):
        return jsonify({
            "reply": "Sorry, I couldn't work out a valid config from that -- could you rephrase?",
            "config": {}, "unsupported": [], "status": "asking",
        })

    unsupported = [u.strip() for u in (parsed.get("unsupported") or []) if isinstance(u, str) and u.strip()][:20]
    return jsonify({
        "reply": parsed.get("reply") or "Got it.",
        "config": parsed.get("config") if isinstance(parsed.get("config"), dict) else {},
        "unsupported": unsupported,
        "status": "ready" if parsed.get("status") == "ready" else "asking",
    })

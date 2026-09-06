"""
trade_matching.py
Trade-execution parsing + FIFO matching, ported from n8n's
"Extract & Match Trades1" code node.

Each closed trade also carries "Fill Count" and "Fills" -- the raw
FIFO-matched fills that were merged/averaged into that trade's single
Entry Price/Exit Price. This is real detail the averaging step would
otherwise throw away (e.g. one entry sold in two pieces at different
exit prices/times): "Fill Count" > 1 means Entry/Exit Price is a
quantity-weighted average, and "Fills" has the individual pieces.

Shared between:
  - /import-trades (CSV import -- this round)
  - the daily IBKR Flex sync pipeline (next round)

The FIFO-matching + same-order-fill-merging logic below is IDENTICAL for
both sources; only the raw-execution parsing differs (CSV rows here vs.
Flex statement XML there, which will get its own parse_flex_executions()
next to this one). Keeping it in one shared module means the daily pipeline
reuses this exact matching code instead of a second copy of it.

Ported faithfully from the original JS, including one of its quirks: the
buy/sell sign-flip below matches on a bare "s" as well as "sell"/"short"
(regex `sell|short|s`, case-insensitive) -- loose, but kept as-is since a
handful of real CSV exports really do just say "S"/"B". Flag rows you're
unsure about by checking Side/Side_normalized in the output if you add
one later.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional


CSV_ALIASES = {
    "symbol": ["symbol", "ticker"],
    "dateTime": ["datetime", "date", "tradedate", "timestamp", "time"],
    "quantity": ["quantity", "qty", "shares", "size"],
    "tradePrice": ["tradeprice", "price", "fillprice", "executionprice"],
    "buySell": ["buysell", "side", "action", "direction"],
    "commission": ["ibcommission", "commission", "fees", "comm"],
}


def _norm_key(k: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k or "").lower())


def _find_csv_field(row: dict, names: list[str]):
    norm_row = {_norm_key(k): v for k, v in row.items()}
    for wanted in names:
        hit = norm_row.get(_norm_key(wanted))
        if hit is not None and hit != "":
            return hit
    return None


def looks_like_csv_row(row: dict) -> bool:
    """A row only counts as a CSV-execution row once it has a symbol +
    something identifiable as a date/time + a price -- deliberately loose
    (aliased headers, not one fixed spelling)."""
    if not isinstance(row, dict):
        return False
    return (
        _find_csv_field(row, CSV_ALIASES["symbol"]) is not None
        and _find_csv_field(row, CSV_ALIASES["dateTime"]) is not None
        and _find_csv_field(row, CSV_ALIASES["tradePrice"]) is not None
    )


def parse_ib_date(s: Any) -> datetime:
    """Handles IBKR's 'YYYYMMDD;HHMMSS' format AND a plain ISO-ish
    'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD' that a hand-made CSV is more
    likely to use. Naive datetime (no timezone attached) -- same as the
    original JS `new Date(...)`, which was implicitly local-server-time."""
    s = str(s or "").strip()
    if ";" in s:
        d, t = s.split(";", 1)
        y, mo, da = d[0:4], d[4:6], d[6:8]
        t = (t or "000000").ljust(6, "0")
        h, mi, sec = t[0:2], t[2:4], t[4:6]
        return datetime.strptime(f"{y}-{mo}-{da}T{h}:{mi}:{sec}", "%Y-%m-%dT%H:%M:%S")
    normalized = s.replace(" ", "T", 1)
    if len(normalized) == 10:
        normalized += "T00:00:00"
    return datetime.fromisoformat(normalized)


def format_time(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def format_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def parse_csv_executions(rows: list[dict]) -> list[dict]:
    """Raw broker-agnostic executions from CSV rows with any reasonable
    header spelling (see CSV_ALIASES). Skips stray/blank/summary rows and
    de-dupes on (symbol, dateTime, buySell, qty, price, commission)."""
    seen: set[str] = set()
    raw_trades: list[dict] = []
    for row in rows:
        if not looks_like_csv_row(row):
            continue
        symbol = _find_csv_field(row, CSV_ALIASES["symbol"])
        date_time = _find_csv_field(row, CSV_ALIASES["dateTime"])
        quantity = _find_csv_field(row, CSV_ALIASES["quantity"])
        trade_price = _find_csv_field(row, CSV_ALIASES["tradePrice"])
        buy_sell = _find_csv_field(row, CSV_ALIASES["buySell"])
        commission = _find_csv_field(row, CSV_ALIASES["commission"]) or 0
        if not symbol or not date_time or not trade_price:
            continue

        try:
            qty = float(quantity)
        except (TypeError, ValueError):
            qty = 0.0
        # Same loose sign-flip as the original JS -- see module docstring.
        if buy_sell and re.search(r"sell|short|s", str(buy_sell).strip(), re.I) and qty > 0:
            qty = -qty

        key = "|".join(str(x) for x in (symbol, date_time, buy_sell, qty, trade_price, commission))
        if key in seen:
            continue
        seen.add(key)
        raw_trades.append({
            "symbol": symbol,
            "dateTime": date_time,
            "quantity": qty,
            "tradePrice": trade_price,
            "commission": commission,
            "_source": "csv",
        })
    return raw_trades


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def fifo_match_and_merge(raw_trades: list[dict], account: Optional[dict] = None) -> list[dict]:
    """Shared FIFO-matching + same-order-fill-merging logic for both the
    CSV import path and the (future) daily Flex path.

    raw_trades: list of {symbol, dateTime, quantity, tradePrice,
    commission, _source} -- dateTime may be a string (parsed here via
    parse_ib_date) or an already-parsed datetime.

    account: optional {user_id, id} for the multi-tenant daily path --
    None for CSV imports, matching the original node's try/catch-to-null
    behavior when it's fed by anything other than 'Loop Broker Accounts'.
    """
    executions = []
    for e in raw_trades:
        try:
            raw_dt = e["dateTime"]
            dt = raw_dt if isinstance(raw_dt, datetime) else parse_ib_date(raw_dt)
            qty = float(e["quantity"])
            price = float(e["tradePrice"])
        except (KeyError, TypeError, ValueError):
            continue
        if not e.get("symbol"):
            continue
        executions.append({
            "symbol": e["symbol"],
            "dateTime": dt,
            "qty": qty,
            "price": price,
            "commission": abs(float(e.get("commission") or 0)),
            "source": e.get("_source"),
        })
    executions.sort(key=lambda ex: ex["dateTime"])

    by_symbol: dict[str, list[dict]] = {}
    for e in executions:
        by_symbol.setdefault(e["symbol"], []).append(e)

    # Raw FIFO-matched fills. A single round-trip trade can legitimately
    # produce several of these when the broker fills an order in more than
    # one piece -- merged back together below.
    raw_matches = []
    for sym, execs in by_symbol.items():
        queue: list[dict] = []
        for ex in execs:
            remaining = ex["qty"]
            while remaining != 0 and queue and _sign(queue[0]["qty"]) != _sign(remaining):
                lot = queue[0]
                match_qty = min(abs(lot["qty"]), abs(remaining))
                direction = 1 if lot["qty"] > 0 else -1
                pnl_before_comm = direction * match_qty * (ex["price"] - lot["price"])
                commission = (
                    lot["commission"] * (match_qty / abs(lot["qty"]))
                    + ex["commission"] * (match_qty / abs(ex["qty"]))
                )
                raw_matches.append({
                    "symbol": sym,
                    "entryDateTime": lot["dateTime"],
                    "exitDateTime": ex["dateTime"],
                    "direction": direction,
                    "matchQty": match_qty,
                    "entryPrice": lot["price"],
                    "exitPrice": ex["price"],
                    "pnlBeforeComm": pnl_before_comm,
                    "commission": commission,
                    "source": ex["source"],
                })
                lot["qty"] -= direction * match_qty
                remaining += direction * match_qty
                if lot["qty"] == 0:
                    queue.pop(0)
            if remaining != 0:
                queue.append({
                    "qty": remaining, "price": ex["price"], "dateTime": ex["dateTime"],
                    "commission": ex["commission"], "source": ex["source"],
                })

    # Merge matches that belong to the same trade (same symbol + entry
    # date + entry time) -- two raw matches sharing that key are fills of
    # ONE order/trade and must be combined before an id is ever built
    # downstream, or they'd collide on the same trade id.
    groups: dict[tuple, list[dict]] = {}
    for m in raw_matches:
        key = (m["symbol"], format_date(m["entryDateTime"]), format_time(m["entryDateTime"]))
        groups.setdefault(key, []).append(m)

    closed_trades = []
    for matches in groups.values():
        first = matches[0]
        total_qty = sum(m["matchQty"] for m in matches)
        entry_price_avg = sum(m["entryPrice"] * m["matchQty"] for m in matches) / total_qty
        exit_price_avg = sum(m["exitPrice"] * m["matchQty"] for m in matches) / total_qty
        pnl_before_comm = sum(m["pnlBeforeComm"] for m in matches)
        commission = sum(m["commission"] for m in matches)
        exit_dt = max(m["exitDateTime"] for m in matches)
        direction = first["direction"]
        source = "daily" if any(m["source"] == "daily" for m in matches) else "csv"

        total_sec = round((exit_dt - first["entryDateTime"]).total_seconds())
        time_in_trade = f"{total_sec // 60:02d}:{total_sec % 60:02d}"

        # The individual raw FIFO matches merged into this trade -- this is
        # exactly what Entry/Exit Price above average away. Sorted by exit
        # time so a scaled-out trade's fills read in the order they filled.
        fills_sorted = sorted(matches, key=lambda m: m["exitDateTime"])
        fills = [{
            "entry_time": format_time(m["entryDateTime"]),
            "entry_price": round(m["entryPrice"], 4),
            "exit_time": format_time(m["exitDateTime"]),
            "exit_price": round(m["exitPrice"], 4),
            "qty": m["matchQty"],
            "pnl_before_comm": round(m["pnlBeforeComm"], 2),
            "commission": round(m["commission"], 2),
        } for m in fills_sorted]

        closed_trades.append({
            "_exitDT": exit_dt,
            "#": 0,
            "Symbol": first["symbol"],
            "Trade Date": format_date(first["entryDateTime"]),
            "Entry Time": format_time(first["entryDateTime"]),
            "Exit Time": format_time(exit_dt),
            "Entry Price": round(entry_price_avg, 4),
            "Exit Price": round(exit_price_avg, 4),
            "Side": "Long" if direction > 0 else "Short",
            "Chg (¢/sh)": round(direction * (exit_price_avg - entry_price_avg) * 100, 2),
            "Time in Trade": time_in_trade,
            "No. of Shares": total_qty,
            "P&L Before Comm": round(pnl_before_comm, 2),
            "Commission": round(commission, 2),
            "P&L After Comm": round(pnl_before_comm - commission, 2),
            "Result": "Win" if (pnl_before_comm - commission) >= 0 else "Loss",
            "Fill Count": len(matches),
            "Fills": fills,
            "Entry Comments": "",
            "Exit Comments": "",
            "Notes": "",
            "Balance Before": "",
            "Balance After": "",
            "_import_source": source,
            "_user_id": account.get("user_id") if account else None,
            "_broker_account_id": account.get("id") if account else None,
        })

    closed_trades.sort(key=lambda t: t["_exitDT"])
    for i, t in enumerate(closed_trades):
        t["#"] = i + 1
        del t["_exitDT"]
    return closed_trades


def parse_flex_executions(flex_doc: dict) -> list[dict]:
    """Raw executions from a parsed IBKR Flex GetStatement response (see
    flex_xml.parse_flex_xml) -- the daily-broker-report counterpart to
    parse_csv_executions() above. Ported from Extract & Match Trades1's
    'daily broker statement' branch, including its de-dupe-by-execID (or a
    field-tuple fallback when execID is missing) and its guard against a
    response that isn't actually a ready Flex statement.

    Raises ValueError with the same actionable message the original JS
    threw for an unrecognized/failed statement, so callers can surface it
    the same way "Extract & Match Trades1"'s own guard did.
    """
    flex_response = flex_doc.get("FlexQueryResponse")
    if not flex_response or not flex_response.get("FlexStatements"):
        if flex_doc.get("FlexStatementResponse"):
            fr = flex_doc["FlexStatementResponse"]
            raise ValueError(
                f"IBKR Flex request failed (Status: {fr.get('Status', 'unknown')}"
                + (f", ErrorCode: {fr['ErrorCode']}" if fr.get("ErrorCode") else "")
                + (f", {fr['ErrorMessage']}" if fr.get("ErrorMessage") else "")
                + "). This should have been caught by the ready-statement poll upstream."
            )
        raise ValueError(
            "parse_flex_executions got data that isn't a ready IBKR Flex statement "
            f"(needs FlexQueryResponse.FlexStatements). Got keys: {list(flex_doc.keys())}."
        )

    seen: set[str] = set()
    raw_trades: list[dict] = []
    statements = as_list_helper(flex_response["FlexStatements"].get("FlexStatement"))
    for stmt in statements:
        trade_confirms = (stmt or {}).get("TradeConfirms") or {}
        confirms = as_list_helper(trade_confirms.get("TradeConfirm"))
        for trade in confirms:
            if not trade:
                continue
            key = trade.get("execID") or "|".join(str(trade.get(k, "")) for k in (
                "symbol", "dateTime", "buySell", "quantity", "price", "commission"))
            if key in seen:
                continue
            seen.add(key)
            raw_trades.append({
                "symbol": trade.get("symbol"),
                "dateTime": trade.get("dateTime"),
                "quantity": trade.get("quantity"),
                "tradePrice": trade.get("price"),
                "commission": trade.get("commission") or 0,
                "_source": "daily",
            })
    return raw_trades


def as_list_helper(value) -> list:
    """Local copy of flex_xml.as_list (avoids a hard import dependency on
    flex_xml from this module -- trade_matching.py stays usable standalone,
    e.g. by /import-trades, without pulling in XML parsing)."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]

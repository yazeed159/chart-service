"""
gappers_store.py
Where scanner.py's premarket scan results land, and where live-service's
engine.py reads them back to resolve a "top_gappers" symbol_rule -- the
piece strategy_store.py's docstring flagged as missing ("top_gappers is
stored... but live's pre-market scanner doesn't exist yet").

Not user-scoped like strategies/backtest_runs -- gappers are market-wide
facts for the day, not anyone's private data, so there's no user_id
column here.

Freshness instead of deletes: scanner.py upserts (on conflict
scan_date+symbol, merge) every poll cycle, so a symbol still gapping gets
its row refreshed and a symbol that's fallen out of the top list just
stops getting touched. get_todays_gappers() below only returns rows
updated within the last few minutes, so a stale symbol ages out on its
own without scanner.py ever needing to issue a delete.

One row per (scan_date, symbol). Run this once in the Supabase SQL editor
before deploying scanner.py:

    create table if not exists gappers (
      id bigserial primary key,
      scan_date date not null,
      symbol text not null,
      price numeric not null,
      gap_pct numeric not null,
      premkt_volume bigint not null default 0,
      updated_at timestamptz not null default now(),
      unique (scan_date, symbol)
    );
    create index if not exists gappers_scan_date_idx on gappers (scan_date);
    alter table gappers enable row level security;
    -- No policies, same reasoning as strategies/backtest_runs: only ever
    -- touched server-side with the service-role key.

Reuses SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY -- no new env vars for
the read side (live-service already has these). scanner.py, running as
its own Render Cron Job rather than inside this repo's web service, needs
its own copy of those two env vars set on that Cron Job service.
"""

from __future__ import annotations

import os
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("chart_service.gappers_store")

ET = ZoneInfo("America/New_York")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

TABLE = "gappers"

# A row not refreshed within this window is treated as no longer
# qualifying (dropped out of the top list, or the scanner stopped for the
# day) -- see the freshness-instead-of-deletes note above. Comfortably
# above scanner.py's poll interval (default 5s) so a normal cycle gap
# never falsely ages a symbol out.
FRESHNESS_WINDOW_MIN = 5


def _headers(extra: dict | None = None) -> dict:
    h = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _require_config():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set -- can't reach gappers")


def upsert_gappers(scan_date: date, rows: list[dict]):
    """rows: [{symbol, price, gap_pct, premkt_volume}, ...]. Called once
    per poll cycle from scanner.py with that cycle's qualifying list --
    see the module docstring for why this is upsert-only, no deletes."""
    if not rows:
        return
    _require_config()
    now = datetime.now(ET).isoformat()
    payload = [
        {
            "scan_date": scan_date.isoformat(),
            "symbol": r["symbol"],
            "price": r["price"],
            "gap_pct": r["gap_pct"],
            "premkt_volume": r["premkt_volume"],
            "updated_at": now,
        }
        for r in rows
    ]
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        params={"on_conflict": "scan_date,symbol"},
        json=payload,
        timeout=20,
    )
    if resp.status_code >= 300:
        log.error("upsert_gappers failed: %s %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()


def get_todays_gappers(top_n: int = 5, min_price: float = 1.0, max_price: float = 50.0,
                        min_gap_pct: float = 5.0, min_dollar_volume: float = 5_000_000) -> list[dict]:
    """Applies a strategy's symbol_rule thresholds against today's fresh
    rows (see FRESHNESS_WINDOW_MIN) and returns the top_n, gap_pct
    descending. Dollar-volume filtering happens here in Python rather
    than in the query since it's price*volume, not a stored column."""
    _require_config()
    today = datetime.now(ET).date()
    cutoff = (datetime.now(ET) - timedelta(minutes=FRESHNESS_WINDOW_MIN)).isoformat()
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={
            "select": "symbol,price,gap_pct,premkt_volume,updated_at",
            "scan_date": f"eq.{today.isoformat()}",
            "updated_at": f"gte.{cutoff}",
            "price": f"gte.{min_price}",
            "gap_pct": f"gte.{min_gap_pct}",
            "order": "gap_pct.desc",
            "limit": "500",  # pre-dollar-volume-filter cap; trimmed to top_n below
        },
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    rows = [r for r in rows if r["price"] <= max_price and r["price"] * r["premkt_volume"] >= min_dollar_volume]
    return rows[:top_n]


def list_todays_gappers(limit: int = 50) -> list[dict]:
    """Raw, unfiltered-by-strategy view of today's scan -- what the
    /gappers page (web-service) shows everyone, as opposed to
    get_symbols_for_rule's per-strategy-threshold view. No freshness cut
    here (unlike get_todays_gappers) -- the page itself shows each row's
    age and a stale/inactive state, since a person looking at the page
    wants to see the scanner stopped, not have stale rows silently
    vanish."""
    _require_config()
    today = datetime.now(ET).date()
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={
            "select": "symbol,price,gap_pct,premkt_volume,updated_at",
            "scan_date": f"eq.{today.isoformat()}",
            "order": "gap_pct.desc",
            "limit": str(limit),
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_symbols_for_rule(rule: dict) -> list[str]:
    """What live-service/engine.py calls to resolve a "top_gappers"
    symbol_rule into an actual symbol list at start_run time. `rule` is
    the same dict strategy_store.py stores under symbol_rule -- see its
    docstring for the shape (top_n, min_price, max_price, min_gap_pct,
    min_dollar_volume)."""
    rows = get_todays_gappers(
        top_n=int(rule.get("top_n", 5)),
        min_price=float(rule.get("min_price", 1.0)),
        max_price=float(rule.get("max_price", 50.0)),
        min_gap_pct=float(rule.get("min_gap_pct", 5.0)),
        min_dollar_volume=float(rule.get("min_dollar_volume", 5_000_000)),
    )
    return [r["symbol"] for r in rows]

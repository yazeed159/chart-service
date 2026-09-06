"""
float_shares_store.py
Persists Polygon's share_class_shares_outstanding lookup (used for the
journal's "float" stat/tag) in Supabase, so it survives restarts/redeploys
instead of living only in chart_service.py's in-memory _float_cache.

Float is no longer fetched automatically for every trade -- see
chart_service.py's /fetch-float route and compute_volume_float_stats'
docstring. It's now a per-symbol, on-demand lookup the user triggers
("Get float" on the trade detail page), and this store is what makes that
still cost Polygon only one real call per symbol, ever, no matter how
many trades or accounts later ask about that same symbol.

Why this exists: _float_cache already avoids a repeat Polygon call for a
symbol within one process's lifetime, but it's wiped on every restart --
so a redeploy would otherwise re-trigger a live /v3/reference/tickers call
the next time anyone asks for a symbol they'd already asked about before.
This table makes it a true one-time cost per symbol, ever (across the
whole app, not per user -- float shares is market data, not something
scoped to one account).

Refreshed every 90 days rather than kept forever -- shares outstanding
rarely changes, but does occasionally (secondary offering, buyback).
get_float_shares() takes a max_age_days and treats an older row as a
miss, forcing exactly one refetch; chart_service.py uses the default.

One row per symbol in a `symbol_float_shares` table. Run this once in the
Supabase SQL editor before deploying:

    create table if not exists symbol_float_shares (
      symbol text primary key,
      shares bigint,
      fetched_at timestamptz not null default now()
    );
    alter table symbol_float_shares enable row level security;
    -- No policies added on purpose: this table is only ever touched by
    -- chart_service.py using the service-role key (which bypasses RLS
    -- entirely, same as backtest_runs/trades/trade_details), never by the
    -- browser directly. RLS is just on so an anon-key client can't
    -- read/write it.

`shares` is nullable and a null row IS a cached result, not a missing one
-- Polygon has no share count for every symbol (thin OTC tickers, indices,
etc.), and re-trying those forever on every trade would defeat the point
of this table. get_float_shares() distinguishes "no row yet" (returns
None) from "row exists, shares unknown" (returns {"shares": None}). Both
are still subject to the same 90-day refresh via fetched_at.

Reuses SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY -- no new env vars.
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("chart_service.float_shares_store")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

TABLE = "symbol_float_shares"
DEFAULT_MAX_AGE_DAYS = 90


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
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set -- can't reach symbol_float_shares")


def get_float_shares(symbol: str, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> dict | None:
    """None means either no row exists yet for this symbol, or its row is
    older than max_age_days -- either way, the caller should hit Polygon
    and then save_float_shares() to refresh it. A dict ({"shares":
    int|None, "fetched_at": str}) means this symbol has a fresh-enough
    lookup already -- including the shares=None case, which is itself a
    cached result and should NOT trigger another Polygon call."""
    _require_config()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={
            "select": "shares,fetched_at",
            "symbol": f"eq.{symbol}",
            "fetched_at": f"gte.{cutoff}",
            "limit": "1",
        },
        timeout=10,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return None
    return {"shares": rows[0].get("shares"), "fetched_at": rows[0].get("fetched_at")}


def save_float_shares(symbol: str, shares: int | None):
    """Upserts one row -- called right after a live Polygon lookup,
    whatever the result (including shares=None, so a symbol Polygon has no
    data for is remembered as such instead of retried every trade). Always
    writes a fresh fetched_at, which is what makes the max_age_days check
    above eventually force a refetch."""
    _require_config()
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        json={"symbol": symbol, "shares": shares, "fetched_at": datetime.now(timezone.utc).isoformat()},
        timeout=10,
    )
    if resp.status_code >= 300:
        log.error("save_float_shares(%s) failed: %s %s", symbol, resp.status_code, resp.text[:500])
        resp.raise_for_status()


def save_float_to_trade(user_id: str, trade_id: str, shares: int | None, float_tag: str):
    """Writes a user-requested float lookup onto one specific trade, so
    re-opening it later shows the float without hitting chart_service.py's
    /fetch-float route (and therefore this module's get_float_shares/
    Polygon path) again. This is the persistence half of /fetch-float --
    get_float_shares()/save_float_shares() above are the symbol-wide cache
    a Polygon lookup itself goes through; this is per-trade and always a
    plain read-modify-write, no caching of its own.

    Touches two tables, both service-role-only (see backtest_runs' and
    this module's own docstrings for why scoping happens in application
    code via explicit user_id/trade_id filters here, not RLS):
      - trade_details.indicators (jsonb) -- merges float_shares/float_tag
        in without disturbing vwap/ema/macd/etc. already stored there.
      - trades.float_tag -- the denormalized copy the journal's filter
        UI and breakdown tables (see app.js) read directly.

    Raises if trade_details has no row for (user_id, trade_id) at all
    (nothing to merge into) or if either write fails -- callers should
    treat that as non-fatal to the request as a whole, since the Polygon
    lookup itself already succeeded by the time this runs."""
    _require_config()

    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/trade_details",
        headers=_headers(),
        params={"select": "indicators", "trade_id": f"eq.{trade_id}", "user_id": f"eq.{user_id}", "limit": "1"},
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        raise RuntimeError(f"no trade_details row for trade_id={trade_id}, user_id={user_id}")

    indicators = dict(rows[0].get("indicators") or {})
    indicators["float_shares"] = shares
    indicators["float_tag"] = float_tag

    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/trade_details",
        headers=_headers({"Prefer": "return=minimal"}),
        params={"trade_id": f"eq.{trade_id}", "user_id": f"eq.{user_id}"},
        json={"indicators": indicators},
        timeout=15,
    )
    resp.raise_for_status()

    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/trades",
        headers=_headers({"Prefer": "return=minimal"}),
        params={"id": f"eq.{trade_id}", "user_id": f"eq.{user_id}"},
        json={"float_tag": float_tag},
        timeout=15,
    )
    resp.raise_for_status()


def list_trades_missing_float(user_id: str) -> list[dict]:
    """Every one of this user's trades that hasn't had a float looked up
    yet -- id + symbol + float_tag, cheap enough to pull in one call and
    feed straight into the bulk "fill missing floats" job (see
    chart_service.py's /fetch-float/bulk/start).

    Reads trades.float_tag rather than trade_details.indicators, since
    float_tag is the denormalized copy save_float_to_trade above always
    keeps in sync (see its docstring) and a trade with no float lookup
    yet has never had that column touched -- it's either null (never
    published with a tag at all) or the "float_unknown" placeholder
    compute_volume_float_stats sets at publish time (see
    chart_service.py's docstring on that). Both count as missing here.

    Scoped to user_id via application-code filter, same as everything
    else in this module -- this table is only ever touched with the
    service-role key, which bypasses RLS entirely."""
    _require_config()
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/trades",
        headers=_headers(),
        params={
            "select": "id,symbol,float_tag",
            "user_id": f"eq.{user_id}",
            "or": "(float_tag.is.null,float_tag.eq.float_unknown)",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json() or []

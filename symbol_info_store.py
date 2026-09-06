"""
symbol_info_store.py
Persists the company-level facts (name, country, sector, a short
description) that daily_sync.py's Gemini verdict prompt used to ask the
model to regenerate on EVERY trade, even for a symbol that had already
been traded (and described) many times before. None of those four
fields depend on the specific trade being graded -- a symbol's name/
country/sector/description doesn't change trade to trade, or really at
all -- so re-asking Gemini for them each time was pure repeated work: a
bigger prompt and a bigger output, with no new information in it.

This makes it a true one-time-per-symbol cost (refreshed occasionally in
case a first answer was thin, see DEFAULT_MAX_AGE_DAYS below), same
pattern as float_shares_store.py: a Supabase table checked before ever
asking Gemini. Unlike that module, though, this doesn't add a live
lookup call of its own -- the first time a symbol IS missing, its info
still comes back as ordinary fields on the SAME verdict call daily_sync.py
already makes (see that file's _get_verdict/_build_verdict_prompt), not
a second, separate Gemini round trip. So this module never increases the
number of LLM calls -- it only stops asking for the same four facts over
and over on trades 2..N of a symbol already on file.

One row per symbol in a `symbol_info` table. Run this once in the
Supabase SQL editor before deploying:

    create table if not exists symbol_info (
      symbol text primary key,
      name text,
      country text,
      sector text,
      description text,
      fetched_at timestamptz not null default now()
    );
    alter table symbol_info enable row level security;
    -- No policies added on purpose, same reasoning as symbol_float_shares:
    -- this table is only ever touched by chart_service.py using the
    -- service-role key (bypasses RLS entirely), never by the browser
    -- directly. RLS is on so an anon-key client still can't read/write it.

Reuses SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY -- no new env vars.
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("chart_service.symbol_info_store")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

TABLE = "symbol_info"
# A company's name/sector/country/description essentially never changes
# -- unlike float_shares_store's 90-day window (which exists for share
# counts that occasionally do move on a secondary offering or buyback),
# this long window is really just a safety net so a thin/blank first-ever
# Gemini answer doesn't stay stuck forever instead of a real caching need.
DEFAULT_MAX_AGE_DAYS = 365


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
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")


def get_symbol_info(symbol: str, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> dict | None:
    """None means no fresh-enough row exists yet -- the caller (see
    daily_sync.py's process_trade) should let this one verdict call also
    ask Gemini for the four fields, then save_symbol_info() the result.
    A dict ({"name", "country", "sector", "description"}) means this
    symbol is already on file and the verdict prompt can skip asking for
    it again this time."""
    _require_config()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={
            "select": "name,country,sector,description",
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
    row = rows[0]
    # A row with nothing useful in it (Gemini came back blank the one
    # time it was asked) is treated as still missing, so a bad first
    # answer doesn't permanently starve this symbol of another attempt.
    if not any(row.get(k) for k in ("name", "country", "sector", "description")):
        return None
    return row


def save_symbol_info(symbol: str, name: str, country: str, sector: str, description: str):
    """Upserts one row, called right after a verdict call that included
    fresh symbol info (daily_sync.py's _get_verdict) -- whatever the
    result, so even a blank/partial answer doesn't get re-asked on the
    very next trade of the same symbol later in the same run (it can
    still be retried on a later day/run -- see get_symbol_info's
    blank-row check above; this just avoids hammering a bad answer
    repeatedly inside one run)."""
    _require_config()
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        json={
            "symbol": symbol, "name": name, "country": country,
            "sector": sector, "description": description,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
        timeout=10,
    )
    if resp.status_code >= 300:
        log.error("save_symbol_info(%s) failed: %s %s", symbol, resp.status_code, resp.text[:500])
        resp.raise_for_status()

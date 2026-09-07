"""
strategy_store.py
Persists saved strategies in Supabase -- the thing that connects backtest
and live-trading. A "strategy" is just a named, reusable bundle of:

  - entry_mode      -- e.g. "ema_dip_reclaim" (same string orb_strategy.py
                        and live-service/engine.py already use)
  - params          -- the entry/stop/exit param dict (stop_mode,
                        target_r, atr settings, etc.) -- same shape as
                        backtest's `strategy_params` and live-service's
                        `param_overrides`
  - symbol_rule      -- how to pick symbols to trade, e.g.
                        {"mode": "manual", "symbols": [...]}  or
                        {"mode": "top_gappers", "top_n": 5,
                         "min_price": 1.0, "max_price": 50.0,
                         "min_dollar_volume": 5000000, "min_gap_pct": 5.0}
                        NOTE: only "manual" is actually usable live today.
                        "top_gappers" is stored (so a strategy saved from
                        a backtest keeps its real scan rule) but live's
                        pre-market scanner doesn't exist yet -- see
                        live-service/engine.py's start_run docstring.

Typically created via POST /backtest/history/<job_id>/save-strategy
(chart_service.py), which copies entry_mode/params straight out of that
run's stored `params` and lets the person confirm a symbol_rule. Read
from both sides: chart_service.py (to list/edit strategies) and
live-service/app.py, which imports this module directly rather than over
HTTP -- see docker-compose.yml's live-service volume mount of this repo
read-only at /chart-service, the same trick engine.py already uses for
`import orb_strategy`.

One row per strategy in a `strategies` table. Run this once in the
Supabase SQL editor before deploying:

    create table if not exists strategies (
      id text primary key,
      user_id uuid not null,
      created_at timestamptz not null default now(),
      name text not null,
      entry_mode text not null,
      params jsonb not null default '{}'::jsonb,
      symbol_rule jsonb not null default '{}'::jsonb,
      source_backtest_run_id text references backtest_runs(id) on delete set null,
      source_summary_stats jsonb not null default '{}'::jsonb
    );
    create index if not exists strategies_user_id_idx on strategies (user_id);
    create index if not exists strategies_created_at_idx on strategies (created_at desc);
    alter table strategies enable row level security;
    -- No policies added on purpose, same reasoning as backtest_runs: this
    -- table is only ever touched server-side with the service-role key
    -- (which bypasses RLS entirely), never queried directly from the
    -- browser with the anon key. RLS is on purely so an anon-key client
    -- can't read/write it. Scoping by account happens in application
    -- code below (every query filters by user_id).

Reuses SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY -- no new env vars.
"""

from __future__ import annotations

import os
import logging

import requests

log = logging.getLogger("chart_service.strategy_store")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

TABLE = "strategies"
LIST_LIMIT = 200


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
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set -- can't reach strategies")


def list_strategies(user_id: str) -> list[dict]:
    """Light list for a strategy picker -- everything except nothing is
    actually heavy here (no report-sized blob like backtest_runs has), so
    this just returns full rows, most recent first, scoped to this
    account."""
    _require_config()
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={
            "select": "id,created_at,name,entry_mode,params,symbol_rule,source_backtest_run_id,source_summary_stats",
            "user_id": f"eq.{user_id}",
            "order": "created_at.desc",
            "limit": str(LIST_LIMIT),
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_strategy(strategy_id: str, user_id: str) -> dict | None:
    """Filtered by id AND user_id -- a valid strategy_id belonging to
    someone else returns None, same as backtest_storage.load_backtest_report."""
    _require_config()
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={"select": "*", "id": f"eq.{strategy_id}", "user_id": f"eq.{user_id}", "limit": "1"},
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def save_strategy(strategy_id: str, user_id: str, created_at: str, name: str, entry_mode: str,
                   params: dict, symbol_rule: dict, source_backtest_run_id: str | None,
                   source_summary_stats: dict) -> dict:
    """Inserts one new strategy row and returns it."""
    _require_config()
    row = {
        "id": strategy_id,
        "user_id": user_id,
        "created_at": created_at,
        "name": name,
        "entry_mode": entry_mode,
        "params": params,
        "symbol_rule": symbol_rule,
        "source_backtest_run_id": source_backtest_run_id,
        "source_summary_stats": source_summary_stats,
    }
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers({"Prefer": "return=representation"}),
        json=row,
        timeout=15,
    )
    if resp.status_code >= 300:
        log.error("save_strategy(%s) failed: %s %s", strategy_id, resp.status_code, resp.text[:500])
        resp.raise_for_status()
    return resp.json()[0]


def delete_strategy(strategy_id: str, user_id: str):
    """Filtered by id AND user_id -- deleting someone else's strategy_id
    is a silent no-op, same as backtest_storage.delete_backtest_run."""
    _require_config()
    resp = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers({"Prefer": "return=minimal"}),
        params={"id": f"eq.{strategy_id}", "user_id": f"eq.{user_id}"},
        timeout=15,
    )
    if resp.status_code >= 300:
        log.error("delete_strategy(%s) failed: %s %s", strategy_id, resp.status_code, resp.text[:500])
        resp.raise_for_status()

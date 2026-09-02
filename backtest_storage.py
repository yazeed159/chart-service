"""
backtest_storage.py
Persists backtest history + full reports in Supabase instead of local
JSON files (backtest_history.json / backtest_reports/<job_id>.json),
which don't survive a redeploy or restart on Render's ephemeral disk.

Deliberately kept SHARED/ANONYMOUS, matching current behavior -- unlike
/import-trades, nothing in /backtest/* knows or asks who's calling it, so
there's no user_id to scope rows by. Every backtest run is visible to
anyone who hits this service, same as it was with local files. (If that
ever needs to change, this is the file to add a user_id column + auth
check to -- see supabase_auth.resolve_user_id for the existing pattern.)

One row per run in a `backtest_runs` table. Run this once in the
Supabase SQL editor before deploying:

    create table if not exists backtest_runs (
      id text primary key,
      created_at timestamptz not null default now(),
      label text,
      params jsonb not null default '{}'::jsonb,
      summary_stats jsonb not null default '{}'::jsonb,
      report jsonb not null default '{}'::jsonb
    );
    create index if not exists backtest_runs_created_at_idx
      on backtest_runs (created_at desc);
    alter table backtest_runs enable row level security;
    -- No policies added on purpose: this table is only ever touched by
    -- chart_service.py using the service-role key (which bypasses RLS
    -- entirely, same as trades/trade_details), never by the browser
    -- directly. RLS is just on so an anon-key client can't read/write it.

`summary_stats` holds the light per-run block the Past Runs list needs
(num_trades, win_rate, profit_factor, ...) -- same fields
_run_backtest_job always computed, just no longer duplicated into a
separate small dict AND the big report; the history list endpoint
selects only the light columns (no `report`) so it stays a cheap read
even with years of runs, matching the old file split's intent without
needing two separate stores.

`report` holds the heavy blob (every trade, full stats incl. equity
curve) -- fetched only when a specific run is opened
(GET /backtest/history/<job_id>/report) or enriched
(POST .../enrich).

Reuses SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY -- no new env vars.
"""

from __future__ import annotations

import os
import logging

import requests

log = logging.getLogger("chart_service.backtest_storage")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

TABLE = "backtest_runs"
HISTORY_LIMIT = 100  # matches the old BACKTEST_HISTORY_MAX -- just caps the list endpoint now, doesn't delete anything


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
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not set -- can't reach backtest_runs")


def save_backtest_run(job_id: str, created_at: str, label: str, params: dict, summary_stats: dict, report: dict):
    """Upserts one full row -- called once, right when a run finishes
    (replaces the old load-all/insert/save-all dance across two files
    with a single insert of the one new row)."""
    _require_config()
    row = {
        "id": job_id,
        "created_at": created_at,
        "label": label,
        "params": params,
        "summary_stats": summary_stats,
        "report": report,
    }
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        json=row,
        timeout=15,
    )
    if resp.status_code >= 300:
        log.error("save_backtest_run(%s) failed: %s %s", job_id, resp.status_code, resp.text[:500])
        resp.raise_for_status()


def load_backtest_history() -> list[dict]:
    """Light list for the Past Runs cards -- id/created_at/label/params/stats
    only, most recent first, same shape backtester.js already expects
    (`stats` key, not `summary_stats`)."""
    _require_config()
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={
            "select": "id,created_at,label,params,summary_stats",
            "order": "created_at.desc",
            "limit": str(HISTORY_LIMIT),
        },
        timeout=15,
    )
    resp.raise_for_status()
    return [
        {
            "id": row["id"],
            "created_at": row["created_at"],
            "label": row.get("label") or "(untitled run)",
            "params": row.get("params") or {},
            "stats": row.get("summary_stats") or {},
        }
        for row in resp.json()
    ]


def load_backtest_report(job_id: str) -> dict | None:
    _require_config()
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={"select": "report", "id": f"eq.{job_id}", "limit": "1"},
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return None
    return rows[0].get("report")


def save_backtest_report(job_id: str, report: dict):
    """Overwrites just the report column -- used by the /enrich callback,
    which loads the report, merges fields onto matched trades, and saves
    it back."""
    _require_config()
    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers({"Prefer": "return=minimal"}),
        params={"id": f"eq.{job_id}"},
        json={"report": report},
        timeout=15,
    )
    if resp.status_code >= 300:
        log.error("save_backtest_report(%s) failed: %s %s", job_id, resp.status_code, resp.text[:500])
        resp.raise_for_status()


def delete_backtest_run(job_id: str):
    _require_config()
    resp = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers({"Prefer": "return=minimal"}),
        params={"id": f"eq.{job_id}"},
        timeout=15,
    )
    if resp.status_code >= 300:
        log.error("delete_backtest_run(%s) failed: %s %s", job_id, resp.status_code, resp.text[:500])
        resp.raise_for_status()

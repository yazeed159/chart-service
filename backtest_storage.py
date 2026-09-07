"""
backtest_storage.py
Persists backtest history + full reports in Supabase instead of local
JSON files (backtest_history.json / backtest_reports/<job_id>.json),
which don't survive a redeploy or restart on Render's ephemeral disk.

Scoped per-user: every row carries the `user_id` of whoever ran the
backtest (resolved server-side in chart_service.py via
supabase_auth.resolve_user_id, same pattern /import-trades already
uses), and every read/write here filters by it. This used to be
deliberately shared/anonymous -- nothing in /backtest/* knew who was
calling it, so everyone's runs showed up in everyone's Past Runs list.
That's what user_id fixes: each account only ever sees, loads, or
deletes its own runs.

One row per run in a `backtest_runs` table. Run this once in the
Supabase SQL editor before deploying (or, if the table already exists
from before this change, run just the ALTER/UPDATE/index/NOT NULL
block below to backfill it):

    create table if not exists backtest_runs (
      id text primary key,
      user_id uuid not null,
      created_at timestamptz not null default now(),
      label text,
      params jsonb not null default '{}'::jsonb,
      summary_stats jsonb not null default '{}'::jsonb,
      report jsonb not null default '{}'::jsonb
    );
    create index if not exists backtest_runs_created_at_idx
      on backtest_runs (created_at desc);
    create index if not exists backtest_runs_user_id_idx
      on backtest_runs (user_id);
    alter table backtest_runs enable row level security;
    -- No policies added on purpose: this table is only ever touched by
    -- chart_service.py using the service-role key (which bypasses RLS
    -- entirely, same as trades/trade_details), never by the browser
    -- directly. RLS is just on so an anon-key client can't read/write it.
    -- Scoping by account happens in application code below (every query
    -- filters by user_id), not via RLS/auth.uid(), since the service-role
    -- key has no notion of auth.uid() to begin with.

    -- If backtest_runs already existed before user_id was added:
    --   alter table backtest_runs add column if not exists user_id uuid;
    --   -- backfill existing rows to some owner before the NOT NULL below,
    --   -- or just delete pre-existing anonymous rows if no real owner is
    --   -- known -- they predate per-account privacy and can't be
    --   -- attributed after the fact:
    --   -- delete from backtest_runs where user_id is null;
    --   alter table backtest_runs alter column user_id set not null;
    --   create index if not exists backtest_runs_user_id_idx on backtest_runs (user_id);

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
(POST .../enrich -- see that function's own docstring for why it does
NOT filter by user_id).

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


def save_backtest_run(job_id: str, user_id: str, created_at: str, label: str, params: dict, summary_stats: dict, report: dict):
    """Upserts one full row -- called once, right when a run finishes
    (replaces the old load-all/insert/save-all dance across two files
    with a single insert of the one new row)."""
    _require_config()
    row = {
        "id": job_id,
        "user_id": user_id,
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


def load_backtest_history(user_id: str) -> list[dict]:
    """Light list for the Past Runs cards -- id/created_at/label/params/stats
    only, most recent first, same shape backtester.js already expects
    (`stats` key, not `summary_stats`). Filtered to this one account's own
    runs -- other users' rows never leave Supabase."""
    _require_config()
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={
            "select": "id,created_at,label,params,summary_stats",
            "user_id": f"eq.{user_id}",
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


def load_backtest_run_light(job_id: str, user_id: str) -> dict | None:
    """Like load_backtest_report, but returns params/summary_stats instead
    of the heavy report blob -- what POST /backtest/history/<job_id>/
    save-strategy needs (a strategy is built from a run's params, not its
    full trade-by-trade report). Filtered by id AND user_id."""
    _require_config()
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={"select": "id,params,summary_stats", "id": f"eq.{job_id}", "user_id": f"eq.{user_id}", "limit": "1"},
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def load_backtest_report(job_id: str, user_id: str) -> dict | None:
    """Filtered by id AND user_id -- a valid job_id belonging to someone
    else's run returns None (the route below turns that into the same 404
    as a job_id that doesn't exist at all, rather than confirming to the
    caller that a run with that id exists but isn't theirs)."""
    _require_config()
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={"select": "report", "id": f"eq.{job_id}", "user_id": f"eq.{user_id}", "limit": "1"},
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return None
    return rows[0].get("report")


def load_backtest_report_unscoped(job_id: str) -> dict | None:
    """Same as load_backtest_report but without the user_id filter -- used
    only by the /enrich callback path (see save_backtest_report's
    docstring for why that path has no user_id to filter by). Every
    person-facing route must go through load_backtest_report above, never
    this one."""
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
    """Overwrites just the report column -- used by the /enrich callback.
    Deliberately NOT filtered by user_id: the caller here is n8n's
    server-to-server workflow (chart_service.py's own /enrich route, hit
    from a callback_url it handed n8n -- see report.js's sendJournal()),
    not the logged-in person's browser, so there's no Supabase access
    token to resolve a user_id from in the first place. The job_id itself
    (an unguessable uuid4().hex[:12], never enumerable via the
    now-scoped /backtest/history list) is what limits this to the one run
    it was generated for."""
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


def delete_backtest_run(job_id: str, user_id: str):
    """Filtered by id AND user_id -- deleting someone else's job_id is a
    silent no-op (0 rows matched) rather than an error, same shape as
    "already gone"."""
    _require_config()
    resp = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers({"Prefer": "return=minimal"}),
        params={"id": f"eq.{job_id}", "user_id": f"eq.{user_id}"},
        timeout=15,
    )
    if resp.status_code >= 300:
        log.error("delete_backtest_run(%s) failed: %s %s", job_id, resp.status_code, resp.text[:500])
        resp.raise_for_status()

# chart-service — trade.log's backend

Flask API deployed on **Render**, backing the `web-service` frontend
(Cloudflare Pages + Supabase). Replaces what used to be an n8n workflow —
every route here has a 1:1 request/response contract with the webhook it
replaced, but there's no n8n in the loop anymore.

## Routes

**Chart generation** (`chart_service.py`)
- `POST /generate-chart` — VWAP/EMA9/EMA20/MACD at entry + a minute-bar
  window for one trade, plus best-effort volume/float context
  (`avg_volume_30d`, `relative_volume`, `float_shares`). Source: Polygon.io
  consolidated tape.
- `POST /generate-daily-chart` — daily bars strictly before a trade date
  (never the trade day itself) + a no-LLM pivot-cluster support/resistance
  guess, for the Support/Resistance feature.
- `POST /tick-data` — real Polygon trade prints for a ≤5-minute window, for
  Rewind's "real ticks" mode. Always returns 200; empty `ticks: []` on any
  failure rather than an error, since the frontend treats "nothing came
  back" and "couldn't reach the server" the same way (falls back to
  simulated ticks).

**Backtester** (`chart_service.py` + `engine.py` / `orb_strategy.py` /
`polygon_client.py`) — auth-gated (`Authorization: Bearer <supabase token>`)
- `POST /backtest/start` → `{job_id}`; `GET /backtest/status/<job_id>`
  (poll); `GET /backtest/defaults`; `GET|DELETE /backtest/history[/<job_id>]`;
  `GET /backtest/history/<job_id>/report`; `POST
  /backtest/history/<job_id>/enrich` (per-trade AI verdicts merged back
  onto one run's own saved report only — never the real journal).
  History/reports are stored in Supabase (`backtest_runs`, scoped by
  `user_id`) — **not** on local disk; Render's disk is ephemeral.

**AI features** (`ai_routes.py`) — same contracts as the n8n webhooks they replaced
- `POST /support-resistance` — Gemini refines/confirms the computed pivot
  levels; falls back to computed-only if the LLM response doesn't parse.
- `POST /trade-chat` — conversational Q&A over the caller's own trade
  history; if a message names a symbol with a real logged trade, pulls
  that trade's indicators in as extra context.
- `POST /backtest-ai` — "Configure with AI" panel on the Backtester tab;
  each turn returns `{reply, config?, status: "asking"|"ready"}`.

**Import** (`import_routes.py`, `backtest_import_routes.py`) — auth-gated
- `POST /import-trades` — CSV upload → FIFO match → chart → AI verdict →
  publish, the same pipeline the daily sync uses. Requires the caller's
  Supabase token (an upload has no other way to know whose data it is).
- `GET /import-trades/<job_id>` / `POST /import-trades/<job_id>/cancel`
- `POST /backtest-import` — "Send to Journal" on a backtest run; responds
  immediately, does the real work in a background thread, merges results
  into that run's own report via `/backtest/history/<job_id>/enrich`.

**Daily sync** (`daily_sync.py` + `publish.py`)
- `POST /daily-sync` — for each connected broker account: pull the IBKR
  Flex report, FIFO-match fills, generate chart + AI verdict per trade,
  publish to Supabase (`trades` + `trade_details`, recomputing
  `equity_after` across the account's full history, not just the new
  batch). Triggered daily by GitHub Actions (`daily-sync.yml`, 18:00 UTC
  = 20:00 Cairo) — the workflow just POSTs and returns; this also doubles
  as a wake-up ping for Render's free tier. Can also be triggered manually
  from the Actions tab (`workflow_dispatch`).

## Auth model

Two different patterns, by design:

- Everything the **frontend reads directly from Supabase** (dashboard,
  journal, stats, etc.) goes through the Supabase JS client with the
  **anon key** — Row Level Security scopes every query to `auth.uid()`
  automatically. No server-side involvement.
- Everything **this service writes**, it writes with the **service role
  key**, which bypasses RLS — because the daily sync writes into
  potentially many different users' rows. So any endpoint that writes on
  behalf of a specific person (`/import-trades`, `/backtest/*`,
  `/backtest-import`) has to know *whose* data it's touching itself,
  rather than relying on RLS to sort it out. That's what
  `supabase_auth.resolve_user_id()` does: takes the `Authorization: Bearer
  <token>` header the browser already has from its own logged-in session,
  verifies it against Supabase's `/auth/v1/user` endpoint, and recovers
  the user id server-side. A forged/expired/missing token is rejected
  with 401 before anything is parsed.
- The daily sync pipeline never needs this — it already knows the user
  from the `broker_accounts` row it's processing.

## Environment variables

| Var | Required | Notes |
|---|---|---|
| `POLYGON_API_KEY` | yes | Consolidated-tape market data |
| `GEMINI_API_KEY` | yes | Vision-LLM verdicts, chat, S/R, backtest-AI |
| `GEMINI_MODEL` | no | default `gemini-3.5-flash-lite` |
| `SUPABASE_URL` | yes | |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | **Never** expose client-side — server only |
| `TELEGRAM_BOT_TOKEN` | no | optional daily-sync failure alerts |
| `PORT` | no | default 5001 (Render sets this itself) |
| `CHART_WINDOW_BEFORE_MIN` / `_AFTER_MIN` | no | default 90 / 30 |
| `CHART_LOOKBACK_DAYS` | no | default 5 (EMA/MACD warm-up) |
| `ENABLE_VOLUME_FLOAT_STATS` | no | default true |
| `VOLUME_STATS_LOOKBACK_DAYS` | no | default 30 |
| `SR_LOOKBACK_DAYS_DEFAULT` | no | default 40 |
| `TICK_DATA_MAX_TRADES` | no | default 2000 |
| `POLYGON_BATCH_SIZE` / `_WINDOW_S` / `_MIN_GAP_S` | no | rate limiting, tuned for Polygon's free tier |
| `IMPORT_RUNS_DIR` / `DAILY_SYNC_RUNS_DIR` | no | local job-progress scratch dirs |

## Deployment

Runs on Render as a standard Python web service (`python chart_service.py`,
reads `PORT` from the environment). `requirements.txt` covers it
(`flask`, `requests`, `pandas`, `mplfinance`, `matplotlib`, `tzdata`).

GitHub Actions (`daily-sync.yml`) hits `POST {RENDER_SERVICE_URL}/daily-sync`
once a day — set the `RENDER_SERVICE_URL` repo secret to your Render
service's base URL.

## Local development (optional)

`start_chart_service.ps1` / `run.bat` are leftover from before this was
deployed on Render: they start Flask locally and tunnel it through ngrok
so a local instance could be reached from outside. Still fine to use for
local testing, but note the URL it prints is **not** the production
Render URL — don't paste it into `config.js` for a real deployment.

Create `polygon.env.ps1` next to the script (already gitignored — see
below) with:
```powershell
$env:POLYGON_API_KEY = "your_polygon_api_key_here"
```

⚠️ **A previous version of this file had a real, live Polygon API key
hardcoded in it and got shared outside this machine. That key should be
treated as compromised — rotate it in your Polygon dashboard and update
whatever's set in your Render environment variables and any local
`polygon.env.ps1`.** Same goes for the Gemini API key if it was ever
hardcoded anywhere outside an environment variable (check any n8n export
before sharing it — see the frontend repo's history notes).

Add a `.gitignore` here with at least:
```
polygon.env.ps1
*.env
ngrok.log
```

## History / cleanup notes

- `backtest_history.json` and `backtest_reports/*.json` used to be the
  on-disk store for backtest history. Both were removed from this repo —
  storage moved to Supabase (`backtest_runs` table, see
  `backtest_storage.py`) because Render's disk doesn't survive a redeploy.
  If you're restoring from an older copy of this repo, don't bring those
  files back; they're not read by anything anymore.
- This service replaces an n8n workflow that used to sit in front of
  Gemini for Support/Resistance, Chat, and Backtest-AI, and a separate
  n8n branch for the daily IBKR sync and CSV import. All of that logic now
  lives here. If an `n8n/chat-workflow.json` export still exists anywhere
  from that era, treat it as sensitive — earlier versions of it had a live
  Gemini key hardcoded in a node's query parameters.

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
-- No policies added on purpose -- same reasoning as strategies/backtest_runs:
-- only ever touched server-side with the service-role key (bypasses RLS).

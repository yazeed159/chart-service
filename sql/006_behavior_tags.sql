-- Behavior tags (session, scaled in/out, added to loser, revenge trade, overtrading, repeat symbol,
-- oversized, entry below VWAP / 9 EMA, gave back winner, winner turned loser, overstayed, ran after exit).
-- NULL = "not tagged yet" (the Journal's "Tag my setups" button fills it for existing trades);
-- an empty array = tagged, nothing applied.
alter table public.trades add column if not exists behavior_tags text[];

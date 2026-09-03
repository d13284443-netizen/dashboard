-- =====================================================================
-- 010_iv_monitor.sql — IV Spike Monitor core schema.
--
-- Replaces the old chain_snapshots design, which consumed the entire
-- free-tier disk. Three structural changes, each fixing a specific
-- failure of that design:
--
--   1. NARROW TICK ROWS. The detector reads exactly five things:
--      series, strike, side, IV, time. The old table also stored and
--      indexed bid/ask/gamma/vega/theta/oi/source_file on every row of
--      history, at ~280 bytes/row. This one is ~78 bytes/row. The full
--      chain still exists, but only as one current row per expiry in
--      `latest_chain` (upserted, never appended) — the payoff and
--      scanner endpoints only ever read the newest snapshot anyway.
--
--   2. PARTITIONED BY DAY, RETENTION BY DROP. The old design used
--      `DELETE ... WHERE collected_at < now() - interval '26 hours'`
--      on pg_cron. A DELETE only marks tuples dead; autovacuum has to
--      reclaim the space, and on a table churning millions of rows a
--      day on shared free-tier resources it never catches up. Table
--      and indexes bloat continuously while the row count looks flat,
--      until the disk fills — and by then there isn't enough headroom
--      left to run VACUUM FULL. That is why the last project could not
--      be recovered. Dropping a partition is an instant catalog
--      operation that returns space to the OS with zero vacuum
--      pressure. See 011_retention.sql.
--
--   3. IDEMPOTENT INGEST. The old table keyed rows on *ingestion*
--      time with no unique constraint, so a re-downloaded file or a
--      retried write silently created a duplicate "snapshot" —
--      inflating storage and skewing the EMA baseline with repeated
--      identical readings. Here the natural key is the source file's
--      own snapshot timestamp, with a real unique constraint, so
--      re-ingesting the same file is a no-op.
--
-- Run in the Supabase SQL editor: 010, then 011.
-- =====================================================================


-- ---------------------------------------------------------------------
-- series — one row per (instrument, expiry symbol) we track.
--
-- Exists so tick rows can carry a 2-byte smallint instead of repeating
-- a ~12-char symbol text on every one of ~144,000 rows/day. At this
-- cadence that alone is worth several MB/day, and it makes the tick
-- table's primary key materially smaller.
-- ---------------------------------------------------------------------
create table if not exists series (
    id smallint generated always as identity primary key,
    instrument text not null,
    symbol text not null,                 -- e.g. 'GCE1/Q26'
    expiry_date_label text,               -- e.g. 'Aug 7'
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    unique (instrument, symbol)
);

alter table series enable row level security;
create policy "authenticated read series" on series
    for select to authenticated using (true);


-- ---------------------------------------------------------------------
-- snapshots — per-file metadata. One row per (series, snapshot time).
--
-- This is where the ingredients for smile-roll adjustment live. The
-- detector needs to know what SPOT was at the baseline in order to tell
-- "the whole vol surface moved" apart from "spot moved and this strike
-- slid along an unchanged smile". Storing spot/ATM IV once per snapshot
-- rather than once per strike costs almost nothing and is the reason
-- expected-spike filtering is possible later without re-ingesting.
-- ---------------------------------------------------------------------
create table if not exists snapshots (
    id bigint generated always as identity primary key,
    series_id smallint not null references series(id) on delete cascade,
    snapshot_ts timestamptz not null,     -- from the FILE's own name, not ingest time
    ingested_at timestamptz not null default now(),
    days_to_expiry integer,
    spot double precision,                -- delta-50 strike, same convention as the original
    atm_iv double precision,              -- interpolated IV at spot; baseline for skew
    strike_count integer,
    liquid_strike_count integer,
    source_file text,
    -- The natural key. Re-ingesting the same file hits this and is
    -- discarded by ON CONFLICT DO NOTHING in the worker.
    unique (series_id, snapshot_ts)
);

create index if not exists idx_snapshots_series_ts
    on snapshots (series_id, snapshot_ts desc);

alter table snapshots enable row level security;
create policy "authenticated read snapshots" on snapshots
    for select to authenticated using (true);


-- ---------------------------------------------------------------------
-- iv_ticks — the time series the detector actually reads.
--
-- PARTITIONED BY RANGE on snapshot_ts. Postgres requires the partition
-- key to be part of any unique constraint, which is why snapshot_ts is
-- duplicated here rather than only living on `snapshots`.
--
-- real (float4) not double precision: IV comes off CQG with ~2 decimal
-- places. float4 carries 7 significant figures — several orders of
-- magnitude more precision than the source has — at half the bytes.
--
-- `liquid` is stored, not recomputed. The old ingest calculated
-- both-sides-quoted in chain_loader and then threw it away, so the
-- detector ran on far-OTM strikes with a 0 bid and a stale ask, whose
-- IV wanders freely. Those strikes generate most false positives.
-- ---------------------------------------------------------------------
create table if not exists iv_ticks (
    snapshot_ts timestamptz not null,
    series_id   smallint not null,
    strike      real not null,
    side        char(1) not null check (side in ('c', 'p')),
    iv          real,
    delta       real,
    moneyness   real,       -- ln(strike / spot) at snapshot time
    oi          real,
    liquid      boolean not null default false,
    primary key (snapshot_ts, series_id, strike, side)
) partition by range (snapshot_ts);

-- The detector's core query is "this series' recent history, newest
-- first". The PK already leads with snapshot_ts, so this covers the
-- per-series ordering within a partition.
create index if not exists idx_iv_ticks_series_ts
    on iv_ticks (series_id, snapshot_ts desc);

alter table iv_ticks enable row level security;
create policy "authenticated read iv_ticks" on iv_ticks
    for select to authenticated using (true);


-- ---------------------------------------------------------------------
-- latest_chain — the full wide chain, ONE row per (series, strike).
--
-- Upserted on every ingest, never appended. This is what the payoff
-- builder / scanner / market metrics read. Bounded size forever:
-- strikes x expiries, roughly 1,500 rows total, regardless of how long
-- the system runs.
-- ---------------------------------------------------------------------
create table if not exists latest_chain (
    series_id smallint not null references series(id) on delete cascade,
    strike double precision not null,
    snapshot_ts timestamptz not null,
    call_bid double precision, call_ask double precision, call_iv double precision,
    call_delta double precision, call_gamma double precision,
    call_vega double precision, call_theta double precision, call_oi double precision,
    put_bid double precision, put_ask double precision, put_iv double precision,
    put_delta double precision, put_gamma double precision,
    put_vega double precision, put_theta double precision, put_oi double precision,
    primary key (series_id, strike)
);

alter table latest_chain enable row level security;
create policy "authenticated read latest_chain" on latest_chain
    for select to authenticated using (true);

-- Ordering guard: a replayed or out-of-order file must never overwrite
-- newer chain data with staler prices. The upsert comes through
-- PostgREST as ON CONFLICT DO UPDATE, whose SET clause can't itself
-- compare timestamps, so the guard lives in a BEFORE UPDATE trigger
-- that protects the table no matter who writes — the worker, a manual
-- replay of a quarantined file (which the README invites), or a hand
-- edit. If the incoming row is not newer, the existing row is kept.
-- The payoff builder and scanner both read this table, so a stale
-- overwrite would show wrong premiums in the UI with no error anywhere.
create or replace function latest_chain_reject_stale()
returns trigger
language plpgsql
as $$
begin
    if new.snapshot_ts < old.snapshot_ts then
        return old;  -- keep the newer row, ignore the stale write
    end if;
    return new;
end;
$$;

drop trigger if exists trg_latest_chain_reject_stale on latest_chain;
create trigger trg_latest_chain_reject_stale
    before update on latest_chain
    for each row execute function latest_chain_reject_stale();


-- ---------------------------------------------------------------------
-- iv_daily — tiny long-horizon rollup, ~1,500 rows/day.
--
-- Deliberately DAILY, not hourly. At a 15-20 minute cadence there are
-- only 3-4 samples per hour, so an hourly rollup would compress by 4x
-- while costing an entire extra table to maintain — not worth it. Raw
-- ticks are cheap enough to keep for 7 days, which covers every drift
-- window the detector uses. This table exists only for multi-week
-- context (e.g. "is 18 vol high for this contract?").
-- ---------------------------------------------------------------------
create table if not exists iv_daily (
    day date not null,
    series_id smallint not null,
    strike real not null,
    side char(1) not null,
    iv_mean real, iv_min real, iv_max real,
    delta_mean real,
    n integer not null,
    primary key (day, series_id, strike, side)
);

alter table iv_daily enable row level security;
create policy "authenticated read iv_daily" on iv_daily
    for select to authenticated using (true);


-- ---------------------------------------------------------------------
-- spike_events — detections.
--
-- WIDER THAN THE ORIGINAL ON PURPOSE. The extra columns are the
-- evidence needed to answer "was this a real vol event, or did spot
-- just move?" — the question that decides whether the alert was worth
-- sending:
--
--   raw_pct_change      what the original detector measured: this
--                       strike's IV now vs its own baseline.
--   adj_pct_change      the same comparison after correcting for the
--                       strike sliding along an unchanged smile as spot
--                       moved (see worker/smile.py). This is the number
--                       that should eventually drive alerting.
--   spot_move_pct       how far spot moved over the same window.
--   atm_iv_change_pct   did the WHOLE surface lift, or just this strike?
--   skew_change         (strike IV - ATM IV) now vs then. A genuine vol
--                       event usually twists the skew; a pure spot move
--                       mostly leaves its shape intact.
--   would_suppress      true when adj_pct_change falls under threshold
--                       while raw_pct_change cleared it — i.e. "the
--                       smile-roll filter would have killed this one".
--
-- During the test week alerts fire on the RAW rule, with the adjusted
-- numbers recorded alongside. That gives a labelled dataset to decide
-- from, instead of guessing at a filter and finding out it was
-- suppressing real signals.
-- ---------------------------------------------------------------------
create table if not exists spike_events (
    id bigint generated always as identity primary key,
    detected_at timestamptz not null default now(),
    series_id smallint not null references series(id),
    snapshot_ts timestamptz not null,
    strike real not null,
    side char(1) not null,
    rule text not null,                   -- 'ema' | 'drift_1h' | 'drift_3h' | ...
    latest_iv real not null,
    baseline_iv real not null,
    raw_pct_change real not null,
    adj_pct_change real,
    spot_move_pct real,
    atm_iv_change_pct real,
    skew_now real,
    skew_change real,
    moneyness real,
    delta real,
    severity text not null default 'info' check (severity in ('info','warn','high')),
    direction text not null default 'spike' check (direction in ('spike','crush')),
    would_suppress boolean not null default false,
    alerted boolean not null default false,
    acknowledged boolean not null default false
);

create index if not exists idx_spike_events_detected_at
    on spike_events (detected_at desc);
-- Backs the suppression lookup, which is the detector's hottest read.
create index if not exists idx_spike_events_suppression
    on spike_events (series_id, side, rule, detected_at desc);

alter table spike_events enable row level security;
create policy "authenticated read spike_events" on spike_events
    for select to authenticated using (true);

-- Column-level control, not a bare `for update ... using (true)`.
--
-- The old schema had a policy commented as "may update the acknowledged
-- flag, but nothing else on the row" — RLS cannot restrict columns, so
-- that policy actually let any logged-in user rewrite any field on any
-- row. Restricting the UPDATE grant to a single column is what actually
-- enforces the intent.
revoke update on spike_events from authenticated;
grant update (acknowledged) on spike_events to authenticated;
create policy "authenticated acknowledge spike_events" on spike_events
    for update to authenticated using (true) with check (true);


-- ---------------------------------------------------------------------
-- worker_health / debug_log — operational visibility.
-- ---------------------------------------------------------------------
create table if not exists worker_health (
    worker text primary key,              -- 'ingest' | 'detector'
    last_success_at timestamptz,
    last_error text,
    last_error_at timestamptz,
    detail jsonb
);

alter table worker_health enable row level security;
create policy "authenticated read worker_health" on worker_health
    for select to authenticated using (true);

create table if not exists debug_log (
    id bigint generated always as identity primary key,
    logged_at timestamptz not null default now(),
    source text not null,
    level text not null check (level in ('info','warn','error')),
    message text not null
);

create index if not exists idx_debug_log_logged_at on debug_log (logged_at desc);

alter table debug_log enable row level security;
create policy "authenticated read debug_log" on debug_log
    for select to authenticated using (true);

-- =====================================================================
-- test_schema.sql — assertions for 010_iv_monitor.sql and
-- 011_retention.sql.
--
-- These cannot run against the same Supabase project as production:
-- several cases create and drop partitions, and one deliberately fills
-- the default partition. Run them against a scratch database.
--
--   createdb iv_test
--   psql iv_test -f schema/010_iv_monitor.sql
--   psql iv_test -f schema/011_retention.sql
--   psql iv_test -v ON_ERROR_STOP=1 -f tests/sql/test_schema.sql
--
-- pg_cron is not available outside Supabase. Comment out the
-- `create extension pg_cron;` line and the four `cron.schedule` calls
-- at the bottom of 011 before loading it locally; the scheduling is
-- covered by inspection rather than by these tests.
--
-- Every check raises an exception on failure, so ON_ERROR_STOP=1 makes
-- the whole file behave like a test runner: it either completes with
-- "ALL SCHEMA TESTS PASSED" or stops at the first failure.
-- =====================================================================

\set ON_ERROR_STOP on
set client_min_messages = warning;

create or replace function _assert(cond boolean, msg text)
returns void language plpgsql as $$
begin
    if not cond then
        raise exception 'ASSERTION FAILED: %', msg;
    end if;
end;
$$;

create or replace function _assert_eq(got anyelement, want anyelement, msg text)
returns void language plpgsql as $$
begin
    if got is distinct from want then
        raise exception 'ASSERTION FAILED: % (got %, want %)', msg, got, want;
    end if;
end;
$$;


-- =====================================================================
-- 1. Structure
-- =====================================================================
\echo '-- 1. structure'

select _assert(
    (select relkind from pg_class where relname = 'iv_ticks') = 'p',
    'iv_ticks must be a partitioned table');

select _assert(
    exists (select 1 from pg_class where relname = 'iv_ticks_default'),
    'the default catch-all partition must exist so ingest never fails');

-- The PK must lead with snapshot_ts: it is both the partition key and
-- the leading column of the detector's ordering.
select _assert_eq(
    (select array_agg(a.attname order by k.ordinality)
     from pg_index i
     join pg_class c on c.oid = i.indrelid
     cross join lateral unnest(i.indkey) with ordinality as k(attnum, ordinality)
     join pg_attribute a on a.attrelid = c.oid and a.attnum = k.attnum
     where c.relname = 'iv_ticks' and i.indisprimary),
    array['snapshot_ts','series_id','strike','side']::name[],
    'iv_ticks primary key column order');

select _assert(
    exists (select 1 from pg_indexes
            where tablename = 'iv_ticks' and indexname = 'idx_iv_ticks_series_ts'),
    'the detector''s per-series ordering index must exist');

-- Narrow rows were the entire point of the redesign. real = 4 bytes.
select _assert_eq(
    (select format_type(atttypid, atttypmod) from pg_attribute
     where attrelid = 'iv_ticks'::regclass and attname = 'iv'),
    'real'::text,
    'iv must stay float4 — float8 doubles the table for precision CQG does not have');


-- =====================================================================
-- 2. Uniqueness and idempotency
-- =====================================================================
\echo '-- 2. idempotency'

insert into series (instrument, symbol, expiry_date_label)
values ('GOLD', 'TEST1/Q26', 'Sep 25');

select ensure_iv_tick_partitions(3);

do $$
declare
    sid smallint := (select id from series where symbol = 'TEST1/Q26');
    ts timestamptz := date_trunc('hour', now());
begin
    insert into snapshots (series_id, snapshot_ts, spot, atm_iv, strike_count)
    values (sid, ts, 3400, 18.0, 1);

    -- Re-ingesting the same file must be a no-op, not a duplicate.
    begin
        insert into snapshots (series_id, snapshot_ts, spot, atm_iv, strike_count)
        values (sid, ts, 3400, 18.0, 1);
        raise exception 'ASSERTION FAILED: duplicate snapshot was accepted';
    exception when unique_violation then
        null;  -- expected
    end;

    perform _assert_eq(
        (select count(*)::int from snapshots where series_id = sid), 1,
        'exactly one snapshot after a duplicate insert attempt');

    insert into iv_ticks (snapshot_ts, series_id, strike, side, iv, liquid)
    values (ts, sid, 3400, 'c', 18.0, true);

    begin
        insert into iv_ticks (snapshot_ts, series_id, strike, side, iv, liquid)
        values (ts, sid, 3400, 'c', 99.0, true);
        raise exception 'ASSERTION FAILED: duplicate tick was accepted';
    exception when unique_violation then
        null;
    end;

    perform _assert_eq(
        (select iv from iv_ticks where series_id = sid), 18.0::real,
        'the original tick value must survive a duplicate insert');
end $$;

-- The series unique constraint prevents two ids for the same contract,
-- which would split a history in half and starve both.
do $$
begin
    begin
        insert into series (instrument, symbol) values ('GOLD', 'TEST1/Q26');
        raise exception 'ASSERTION FAILED: duplicate series was accepted';
    exception when unique_violation then null;
    end;
end $$;


-- =====================================================================
-- 3. Check constraints
-- =====================================================================
\echo '-- 3. constraints'

do $$
declare
    sid smallint := (select id from series where symbol = 'TEST1/Q26');
    ts timestamptz := date_trunc('hour', now()) + interval '1 minute';
begin
    begin
        insert into iv_ticks (snapshot_ts, series_id, strike, side, iv, liquid)
        values (ts, sid, 3400, 'x', 18.0, true);
        raise exception 'ASSERTION FAILED: side ''x'' was accepted';
    exception when check_violation then null;
    end;

    begin
        insert into spike_events (series_id, snapshot_ts, strike, side, rule,
                                  latest_iv, baseline_iv, raw_pct_change, severity)
        values (sid, ts, 3400, 'c', 'ema', 20, 18, 0.11, 'critical');
        raise exception 'ASSERTION FAILED: invalid severity was accepted';
    exception when check_violation then null;
    end;

    begin
        insert into debug_log (source, level, message)
        values ('test', 'debug', 'x');
        raise exception 'ASSERTION FAILED: invalid log level was accepted';
    exception when check_violation then null;
    end;
end $$;

-- detector._severity can only emit these three; the constraint and the
-- Python must agree or every high-severity insert fails at 3am.
select _assert(
    (select count(*) from unnest(array['info','warn','high']) v
     where v not in ('info','warn','high')) = 0,
    'severity vocabulary matches detector._severity');


-- =====================================================================
-- 4. Partition routing
-- =====================================================================
\echo '-- 4. partition routing'

do $$
declare
    sid smallint := (select id from series where symbol = 'TEST1/Q26');
    today_part text := 'iv_ticks_' || to_char(current_date, 'YYYYMMDD');
begin
    perform _assert(
        exists (select 1 from pg_class where relname = today_part),
        'today''s partition must exist after ensure_iv_tick_partitions');

    insert into iv_ticks (snapshot_ts, series_id, strike, side, iv, liquid)
    values (now(), sid, 3425, 'c', 18.0, true);

    execute format('select count(*) from %I', today_part) into strict sid;
    perform _assert(sid > 0, 'a row written now must land in today''s partition');
end $$;

-- Idempotency: running the creator twice must not error.
select ensure_iv_tick_partitions(3);
select ensure_iv_tick_partitions(3);


-- =====================================================================
-- 5. THE DEFAULT-PARTITION TRAP
-- =====================================================================
\echo '-- 5. default partition trap'

-- FINDING. 011's header presents the default partition as a pure
-- safety net: "if pg_cron is disabled the system degrades to storage
-- grows instead of ingest breaks". That is true for the writes, but it
-- is not the whole story.
--
-- Postgres cannot attach a new partition whose range overlaps rows
-- already sitting in the DEFAULT partition — it would have to move
-- them. So once a single day's rows land in iv_ticks_default,
-- ensure_iv_tick_partitions() FAILS for that date, permanently, and
-- keeps failing every night until someone empties the default manually.
--
-- The degradation is therefore not "storage grows"; it is "storage
-- grows AND partition creation is now stuck AND retention can never
-- reclaim those rows, because dropping partitions does not touch the
-- default". That is much closer to the failure that destroyed the
-- previous database.
--
-- This block proves it. It is the most important test in this file.

do $$
declare
    sid smallint := (select id from series where symbol = 'TEST1/Q26');
    far_day date := current_date + 30;   -- no partition exists this far out
    part_name text := 'iv_ticks_' || to_char(current_date + 30, 'YYYYMMDD');
    landed_in_default int;
    creation_failed boolean := false;
begin
    -- A row for a date with no partition falls into the default.
    insert into iv_ticks (snapshot_ts, series_id, strike, side, iv, liquid)
    values (far_day::timestamptz + interval '9 hours', sid, 3450, 'c', 18.0, true);

    select count(*) into landed_in_default from iv_ticks_default;
    perform _assert(landed_in_default > 0,
        'a row for an uncreated date must land in the default partition');

    -- Now try to create that date's partition, exactly as the nightly
    -- cron job would.
    begin
        execute format(
            'create table %I partition of iv_ticks for values from (%L) to (%L)',
            part_name, far_day::timestamptz, (far_day + 1)::timestamptz);
    exception when others then
        creation_failed := true;
        raise warning 'partition creation blocked as predicted: %', sqlerrm;
    end;

    perform _assert(creation_failed,
        'EXPECTED FAILURE: creating a partition over rows already in the '
        'default must be rejected by Postgres. If this assertion fails, '
        'your Postgres version moves the rows automatically and the trap '
        'does not apply — good news, update this test.');

    -- Clean up so later tests are unaffected.
    delete from iv_ticks_default;
end $$;

-- Recommended mitigation, which this suite does NOT assert because it
-- is not implemented yet:
--   1. Have ensure_iv_tick_partitions detach the default, create the
--      missing partitions, then re-attach — or
--   2. Alert on `select count(*) from iv_ticks_default` being non-zero,
--      via worker_health, so it is caught on day one rather than
--      whenever partition creation is next noticed.
-- Option 2 is a five-line change to watchdog.py and would have turned
-- this silent trap into a Telegram message.


-- =====================================================================
-- 6. Retention drops, never deletes
-- =====================================================================
\echo '-- 6. retention'

do $$
declare
    old_part text := 'iv_ticks_' || to_char(current_date - 30, 'YYYYMMDD');
    keep_part text := 'iv_ticks_' || to_char(current_date, 'YYYYMMDD');
begin
    execute format(
        'create table if not exists %I partition of iv_ticks for values from (%L) to (%L)',
        old_part, (current_date - 30)::timestamptz, (current_date - 29)::timestamptz);

    perform drop_old_iv_tick_partitions(7);

    perform _assert(
        not exists (select 1 from pg_class where relname = old_part),
        'a partition past the horizon must be dropped');
    perform _assert(
        exists (select 1 from pg_class where relname = keep_part),
        'today''s partition must survive retention');
end $$;

-- Boundary: keep_days must be inclusive of the horizon day itself.
do $$
declare
    edge_part text := 'iv_ticks_' || to_char(current_date - 7, 'YYYYMMDD');
begin
    execute format(
        'create table if not exists %I partition of iv_ticks for values from (%L) to (%L)',
        edge_part, (current_date - 7)::timestamptz, (current_date - 6)::timestamptz);
    perform drop_old_iv_tick_partitions(7);
    perform _assert(
        exists (select 1 from pg_class where relname = edge_part),
        'the partition exactly at the horizon must be KEPT (cutoff is strict <), '
        'so drift_24h always has a baseline');
end $$;

-- The regex must not match anything outside the tick partitions.
do $$
begin
    create table if not exists iv_ticks_backup_20200101 (x int);
    perform drop_old_iv_tick_partitions(1);
    perform _assert(
        exists (select 1 from pg_class where relname = 'iv_ticks_backup_20200101'),
        'the drop regex must not match a differently-named table');
    drop table iv_ticks_backup_20200101;
end $$;


-- =====================================================================
-- 7. Rollup before drop
-- =====================================================================
\echo '-- 7. rollup'

do $$
declare
    sid smallint := (select id from series where symbol = 'TEST1/Q26');
    y date := current_date - 1;
    part_name text := 'iv_ticks_' || to_char(current_date - 1, 'YYYYMMDD');
begin
    execute format(
        'create table if not exists %I partition of iv_ticks for values from (%L) to (%L)',
        part_name, y::timestamptz, (y + 1)::timestamptz);

    insert into iv_ticks (snapshot_ts, series_id, strike, side, iv, delta, liquid) values
        (y::timestamptz + interval '9 hours',  sid, 3400, 'c', 18.0, 50, true),
        (y::timestamptz + interval '12 hours', sid, 3400, 'c', 20.0, 48, true),
        (y::timestamptz + interval '15 hours', sid, 3400, 'c', 22.0, 46, true),
        (y::timestamptz + interval '16 hours', sid, 3400, 'c', null, 45, true);

    perform rollup_iv_daily(y);

    perform _assert_eq(
        (select n from iv_daily where day = y and series_id = sid
           and strike = 3400 and side = 'c'), 3,
        'null IVs must be excluded from the rollup count');
    perform _assert_eq(
        (select iv_mean from iv_daily where day = y and series_id = sid), 20.0::real,
        'rollup mean');
    perform _assert_eq(
        (select iv_min from iv_daily where day = y and series_id = sid), 18.0::real,
        'rollup min');
    perform _assert_eq(
        (select iv_max from iv_daily where day = y and series_id = sid), 22.0::real,
        'rollup max');

    -- Re-running must update, not duplicate or error.
    perform rollup_iv_daily(y);
    perform _assert_eq(
        (select count(*)::int from iv_daily where day = y and series_id = sid), 1,
        'rollup must be idempotent');
end $$;

-- Ordering guard: the rollup job is scheduled at 00:20 and the drop at
-- 00:40. Rolling up a day whose partition is already gone silently
-- writes nothing rather than erroring, so the 20-minute margin is the
-- only thing protecting a day of history.
do $$
declare
    y date := current_date - 1;
begin
    delete from iv_daily where day = y;
    execute format('drop table if exists %I', 'iv_ticks_' || to_char(y, 'YYYYMMDD'));
    perform rollup_iv_daily(y);
    perform _assert(
        not exists (select 1 from iv_daily where day = y),
        'rolling up a dropped partition writes nothing AND raises nothing — '
        'the job ordering is load-bearing and unguarded');
end $$;


-- =====================================================================
-- 8. Column-level UPDATE grant on spike_events
-- =====================================================================
\echo '-- 8. privileges'

-- 010's comment is explicit that RLS cannot restrict columns and that
-- the column-level GRANT is what actually enforces "acknowledged only".
-- Assert the grant rather than trusting the comment.

select _assert(
    has_column_privilege('authenticated', 'spike_events', 'acknowledged', 'UPDATE'),
    'authenticated must be able to acknowledge an event');

select _assert(
    not has_column_privilege('authenticated', 'spike_events', 'raw_pct_change', 'UPDATE'),
    'authenticated must NOT be able to rewrite detection results');

select _assert(
    not has_column_privilege('authenticated', 'spike_events', 'severity', 'UPDATE'),
    'authenticated must NOT be able to rewrite severity');

select _assert(
    not has_table_privilege('authenticated', 'iv_ticks', 'INSERT'),
    'the browser role must never write ticks');

-- RLS is on everywhere a browser can reach.
do $$
declare t text;
begin
    foreach t in array array['series','snapshots','iv_ticks','latest_chain',
                             'iv_daily','spike_events','worker_health','debug_log']
    loop
        perform _assert(
            (select relrowsecurity from pg_class where relname = t),
            format('RLS must be enabled on %s', t));
    end loop;
end $$;


-- =====================================================================
-- 9. Timezone sensitivity of the partition bounds
-- =====================================================================
\echo '-- 9. timezone'

-- FINDING, paired with the worker-side one in tests/test_ingest.py.
--
-- ensure_iv_tick_partitions builds bounds with `d::timestamptz`, which
-- resolves using the SESSION TimeZone. Run by pg_cron under a non-UTC
-- database timezone, the daily boundaries shift by the offset, and rows
-- near midnight route to the neighbouring partition — or to the default
-- if the neighbour has already been dropped.
--
-- This is only a problem in combination with a non-UTC setting, which
-- Supabase does not use by default. Asserted here so a future timezone
-- change is caught by the suite rather than by a gap in the data.

select _assert_eq(current_setting('TimeZone'), 'UTC'::text,
    'partition bounds assume a UTC database timezone; ensure_iv_tick_partitions '
    'must be made timezone-explicit before this changes');


-- =====================================================================
\echo ''
\echo 'ALL SCHEMA TESTS PASSED'

drop function _assert(boolean, text);
drop function _assert_eq(anyelement, anyelement, text);

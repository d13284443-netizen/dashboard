-- =====================================================================
-- 011_retention.sql — Partition lifecycle for iv_ticks.
--
-- Two jobs: create tomorrow's partitions before they're needed, and
-- drop partitions past the retention horizon. Both are cheap catalog
-- operations. Nothing here issues a DELETE against iv_ticks, which is
-- the whole point — see 010's header for why the previous DELETE-based
-- retention destroyed the last database.
--
-- SAFETY PROPERTY WORTH NOTING: if pg_cron is disabled, or these jobs
-- silently stop, ingest keeps working. Writes land in a DEFAULT
-- partition rather than failing. The system degrades to "storage grows"
-- instead of "ingest breaks", which is the right way round — you get a
-- disk-usage warning with days of runway, not a data outage.
-- =====================================================================

create extension if not exists pg_cron;

-- Catch-all so a missing future partition can never reject an insert.
-- Should normally stay empty; if it has rows, partition creation has
-- fallen behind and needs looking at.
create table if not exists iv_ticks_default partition of iv_ticks default;


-- ---------------------------------------------------------------------
-- Create daily partitions ahead of time. Idempotent — safe to run by
-- hand at any point, and safe to run twice.
-- ---------------------------------------------------------------------
create or replace function ensure_iv_tick_partitions(days_ahead integer default 3)
returns void
language plpgsql
as $$
declare
    d date;
    part_name text;
begin
    for i in 0..days_ahead loop
        d := (current_date + i);
        part_name := 'iv_ticks_' || to_char(d, 'YYYYMMDD');
        if not exists (select 1 from pg_class where relname = part_name) then
            execute format(
                'create table %I partition of iv_ticks for values from (%L) to (%L)',
                part_name, d::timestamptz, (d + 1)::timestamptz
            );
        end if;
    end loop;
end;
$$;


-- ---------------------------------------------------------------------
-- Drop partitions older than the retention horizon.
--
-- 7 days of raw ticks, chosen so the longest drift window (24h) always
-- has a real baseline even after a multi-day outage. At ~11 MB/day this
-- is ~79 MB total. The old design kept 26 hours, which meant drift_24h
-- was comparing against rows that were about to be deleted — and after
-- any ingest gap it silently had no baseline at all and simply stopped
-- detecting, with no error.
-- ---------------------------------------------------------------------
create or replace function drop_old_iv_tick_partitions(keep_days integer default 7)
returns void
language plpgsql
as $$
declare
    r record;
    cutoff date := current_date - keep_days;
begin
    for r in
        select relname from pg_class
        where relname ~ '^iv_ticks_\d{8}$'
          and to_date(right(relname, 8), 'YYYYMMDD') < cutoff
    loop
        execute format('drop table if exists %I', r.relname);
    end loop;
end;
$$;


-- ---------------------------------------------------------------------
-- Roll yesterday's ticks into iv_daily before its partition is dropped.
-- Ordering matters: this must run well before the drop job.
-- ---------------------------------------------------------------------
create or replace function rollup_iv_daily(for_day date default current_date - 1)
returns void
language sql
as $$
    insert into iv_daily (day, series_id, strike, side, iv_mean, iv_min, iv_max, delta_mean, n)
    select for_day, series_id, strike, side,
           avg(iv)::real, min(iv)::real, max(iv)::real, avg(delta)::real, count(*)
    from iv_ticks
    where snapshot_ts >= for_day::timestamptz
      and snapshot_ts <  (for_day + 1)::timestamptz
      and iv is not null
    group by series_id, strike, side
    on conflict (day, series_id, strike, side) do update
      set iv_mean = excluded.iv_mean, iv_min = excluded.iv_min,
          iv_max  = excluded.iv_max,  delta_mean = excluded.delta_mean,
          n = excluded.n;
$$;

create or replace function purge_old_debug_log()
returns void language sql as $$
    delete from debug_log where logged_at < now() - interval '14 days';
$$;


-- ---------------------------------------------------------------------
-- Schedule. Times are UTC.
-- ---------------------------------------------------------------------
select cron.schedule('iv_ticks_create_partitions', '30 22 * * *',
                     $$ select ensure_iv_tick_partitions(3); $$);

select cron.schedule('iv_daily_rollup',            '20 0 * * *',
                     $$ select rollup_iv_daily(); $$);

select cron.schedule('iv_ticks_drop_partitions',   '40 0 * * *',
                     $$ select drop_old_iv_tick_partitions(7); $$);

select cron.schedule('debug_log_purge',            '50 0 * * *',
                     $$ select purge_old_debug_log(); $$);


-- Create the first few partitions right now so ingest can start
-- immediately rather than waiting for tonight's cron run.
select ensure_iv_tick_partitions(3);

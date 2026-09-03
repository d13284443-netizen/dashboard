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
    default_has_rows boolean;
begin
    -- Is anything sitting in the default partition? If so, creating a
    -- partition whose range overlaps those rows would be REJECTED by
    -- Postgres, jamming partition creation for that date permanently —
    -- the trap the test suite (§5) and 011's header both flag. When the
    -- default is non-empty we take the safe, slightly heavier path:
    -- detach it, create the partitions, migrate any overlapping rows out
    -- of the detached table into their proper partition, then reattach.
    select exists (select 1 from iv_ticks_default limit 1) into default_has_rows;

    if default_has_rows then
        alter table iv_ticks detach partition iv_ticks_default;
    end if;

    for i in 0..days_ahead loop
        d := (current_date + i);
        part_name := 'iv_ticks_' || to_char(d, 'YYYYMMDD');
        if not exists (select 1 from pg_class where relname = part_name) then
            execute format(
                'create table %I partition of iv_ticks for values from (%L) to (%L)',
                part_name, d::timestamptz, (d + 1)::timestamptz
            );
            -- Move any rows that were trapped in the default for this
            -- date into the partition that now owns their range. Cheap
            -- when the default is empty (the common case never even
            -- reaches this branch); bounded by one day of rows otherwise.
            if default_has_rows then
                execute format(
                    'with moved as (delete from iv_ticks_default '
                    || 'where snapshot_ts >= %L and snapshot_ts < %L returning *) '
                    || 'insert into %I select * from moved',
                    d::timestamptz, (d + 1)::timestamptz, part_name
                );
            end if;
        end if;
    end loop;

    if default_has_rows then
        alter table iv_ticks attach partition iv_ticks_default default;
    end if;
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
    part_day date;
    cutoff date := current_date - keep_days;
    rolled boolean;
begin
    for r in
        select relname from pg_class
        where relname ~ '^iv_ticks_\d{8}$'
          and to_date(right(relname, 8), 'YYYYMMDD') < cutoff
    loop
        part_day := to_date(right(r.relname, 8), 'YYYYMMDD');
        -- Never drop a day that wasn't rolled into iv_daily first. The
        -- rollup and drop are separate cron jobs; if the rollup failed
        -- or was skipped, dropping here would silently erase that day's
        -- history forever. Verify a rollup exists; if not, roll it up
        -- now, then only drop on confirmed success.
        select exists (select 1 from iv_daily where day = part_day) into rolled;
        if not rolled then
            perform rollup_iv_daily(part_day);
            select exists (select 1 from iv_daily where day = part_day) into rolled;
        end if;
        if rolled then
            execute format('drop table if exists %I', r.relname);
        else
            raise warning 'iv_ticks partition % not dropped: no iv_daily rollup for % (kept to avoid data loss)',
                r.relname, part_day;
        end if;
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
-- iv_ticks_default_count — how many rows are trapped in the default
-- partition. Should always be 0. A non-zero value means partition
-- creation fell behind and rows landed in the catch-all; the watchdog
-- polls this and alerts, and ensure_iv_tick_partitions() will migrate
-- them out on its next run. Exposed as SECURITY DEFINER so the worker
-- can call it via PostgREST RPC with the service key.
-- ---------------------------------------------------------------------
create or replace function iv_ticks_default_count()
returns bigint
language sql
security definer
as $$
    select count(*) from iv_ticks_default;
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

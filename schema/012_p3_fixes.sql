-- =====================================================================
-- 012_p3_fixes.sql — additive migration for the P3-round changes.
--
-- Safe to run on an existing database: only ADDs a column and is
-- idempotent (IF NOT EXISTS). Does not touch existing rows' data. Run
-- this once in the Supabase SQL editor after deploying the P3 fixes;
-- new installs get the column from 010 directly and can skip this.
-- =====================================================================

-- Vol-crush support: events now carry a direction. Existing rows are
-- all spikes (the only thing the old detector produced), so the default
-- backfills them correctly.
alter table spike_events
    add column if not exists direction text not null default 'spike';

-- Enforce the allowed values. Dropped-and-recreated so re-running is
-- safe even if a prior run added it.
alter table spike_events drop constraint if exists spike_events_direction_check;
alter table spike_events
    add constraint spike_events_direction_check check (direction in ('spike', 'crush'));

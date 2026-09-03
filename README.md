# IV Spike Monitor — rebuild

Standalone IV monitor. No web app yet: data flows VPS → Supabase →
Telegram. The payoff builder and scanner can be layered on later against
the same `latest_chain` table, which is already being populated.

## Setup, in order

**1. Supabase.** Create a project. In the SQL editor, run
`schema/010_iv_monitor.sql`, then `schema/011_retention.sql`. The second
one needs `pg_cron` — enable it under Database → Extensions first if the
`create extension` line errors.

Verify partitions exist:

```sql
select relname from pg_class where relname like 'iv_ticks_2%' order by 1;
```

You should see four dated tables. If not, run `select
ensure_iv_tick_partitions(3);` and check for an error.

**2. VPS.** Copy `worker/` to the VPS. Then:

```powershell
pip install -r requirements.txt
copy .env.example .env
notepad .env          # fill in Supabase URL + SERVICE key, Telegram, WATCH_DIR
python -u ingest.py   # run in the foreground first
```

Watch one download cycle land before installing services. You want to
see a line like `GCE1/Q26 @ 09:15 — 247 strikes, 132 liquid, spot 3418.7`.
The `liquid` number counts liquid *legs* (call and put sides counted
separately), so its ceiling is `strikes × 2`, not `strikes`. The
dashboard's Liquid stat shows it as `liquid / (strikes × 2)`. If that
ratio is a small fraction, the liquidity gate is too tight for your
chain — loosen `MIN_OI` or `MAX_SPREAD_PCT` in `.env`.

**3. Services.** Once the foreground run looks right:

```powershell
choco install nssm
.\install_services.ps1 -PythonExe "C:\Python312\python.exe" -WorkerDir "C:\iv\worker"
```

Chrome stays outside this — it needs an interactive desktop session.
The services only read the folder Chrome writes into.

## Architecture

```
Chrome + extension  ──►  Downloads\options-*.xlsx
                             │
                      ingest.py (service)
                             │  parse, gate on liquidity, compute spot + ATM IV
                             ├──►  snapshots     (one row per file)
                             ├──►  iv_ticks      (partitioned by day, 7d retention)
                             └──►  latest_chain  (upserted, bounded size)
                             │
                      detector.py (inline, on arrival)
                             │  time-weighted EMA + drift windows
                             │  smile-roll adjustment
                             └──►  spike_events  ──►  Telegram

                      watchdog.py (service) ──► Telegram on staleness
```

Detection runs **when a file arrives**, not on a timer. At a 15–20 minute
cadence a 30-second timer would recompute an unchanged answer ~40 times
per new datapoint.

## Storage

The previous database filled its disk and could not be recovered. Two
causes, both fixed:

Retention used `DELETE`, which only marks tuples dead — autovacuum has
to reclaim the space and never caught up on free-tier resources. Table
and indexes bloated while row counts looked flat, until there wasn't
enough headroom left to run `VACUUM FULL`. Retention here drops daily
partitions instead, which returns space to the OS instantly.

Rows were also far wider than the detector needed: bid/ask/greeks/oi/
filename stored and indexed on every historical row at ~280 bytes, when
the detector reads only IV. Ticks are now ~78 bytes; the full chain
lives in `latest_chain` as one row per strike, upserted forever.

Budget at your cadence:

| | rows/day | size |
|---|---|---|
| `iv_ticks` @ 15 min | ~144,000 | ~11 MB/day |
| `iv_ticks` 7-day steady state | | **~79 MB** |
| `latest_chain` | — | ~1 MB, fixed |
| `iv_daily` | ~1,500 | ~0.12 MB/day |

Comfortable inside the 500 MB free tier. Keep an eye on Database → Usage
for the first few days anyway; if your chains carry many more strikes
than the 250 assumed here, scale that first row accordingly.

## Detector

| | old | now |
|---|---|---|
| history fetch | `asc` + `limit 50000` — silently returned stale rows past ~33 snapshots and stopped detecting | paged, `desc`, cannot truncate the recent end |
| EMA baseline | `span=12` samples ≈ 3–4h at this cadence, drifting with the interval | time-weighted, 90-min half-life |
| liquidity | not filtered | both-sided quote, OI, spread, delta band |
| suppression | exact strike — one event re-fired across 3405/3410/3415 | ~1% moneyness buckets |
| spot moves | read as vol events | corrected, see below |

## The expected-spike problem

Half-solved, deliberately.

**Solved: smile roll.** A fixed strike does not stay in the same place
on the vol curve. When spot moves, a strike moving away from the money
slides up the convex wing and its IV rises with no vol information in
it. The old detector read every one of those as a spike. `smile.py`
compares at constant moneyness instead: it finds the strike that
occupied this strike's current position on the curve at baseline time,
and measures against that. In testing, a pure 1.5% spot move producing a
**+13.5% raw IV change** comes out at **−0.03% adjusted**, while a
genuine 15% surface lift still reads +15.0%.

**Not yet solved: spot–vol correlation.** Gold has genuinely positive
spot–vol correlation — rallies really are vol events, so ATM vol rising
on a rally can be both real *and* expected. That is a different thing
from smile roll and needs a different fix: a spot–vol beta, estimated
from history, so the alert fires on the residual rather than the raw
move. It can't be built yet because estimating a beta requires history
this database doesn't have. Every event already stores `spot_move_pct`
and `atm_iv_change_pct`, so the week of data being collected now is
exactly the input needed. This is the natural week-2 task.

**Shadow mode.** For the test week, alerts fire on the raw rule and the
adjusted numbers are recorded alongside. Groups the filter would have
dropped are still sent, marked *"Consistent with smile roll — would be
filtered once shadow mode ends."* The point is to see what the filter
would have taken away before trusting it. After a week:

```sql
select rule, side,
       count(*)                                  as fired,
       count(*) filter (where would_suppress)     as would_filter,
       round(100.0 * count(*) filter (where would_suppress) / count(*), 1) as pct_filtered
from spike_events
group by rule, side
order by fired desc;
```

If `pct_filtered` is high and the filtered ones look like noise when you
check them against what the market did, set `ALERT_ON_RAW=false`.

## Operations

- Logs: `C:\iv\logs\iv-ingest.log`, or the `debug_log` table.
- Health: `select * from worker_health;`
- Files that failed to parse are moved to `Downloads\failed\`, never
  deleted. If that folder fills up, the chain format has changed and
  `chain_loader.py`'s column mapping needs checking.
- Tests: `python test_detector.py` — pure math, no database needed.

## Tuning

`SPIKE_THRESHOLD_PCT=0.10` is inherited from the original and is a
guess, not a calibration. Expect to move it in week 1. Too many alerts →
raise it, or drop `drift_1h` (only ~3 samples at this cadence, so it is
the noisiest window). Too few → lower to 0.07 and check what appears.

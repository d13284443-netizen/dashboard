# IV Spike Monitor — automated test suite

361 cases across Python, JavaScript and SQL. Written against the code as
uploaded; every defect listed below was found by writing the test, not
by reading alone.

```
./tests/run-tests.sh          # Python + JavaScript, ~12s, no secrets needed
./tests/run-tests.sh py
./tests/run-tests.sh js
```

The SQL suite is excluded from the runner on purpose — it creates and
drops partitions and must never be pointed at production. Run it by hand
against a scratch database; instructions are in the file header.

**Prerequisites:** `pip install pytest openpyxl requests python-dotenv`,
Node 18+ (uses the built-in `node:test`), and for the scanner suite the
`__test` seam in `tests/patches/scanner-test-seam.diff`.

---

## 1. Before anything else: rotate your keys

`worker/.env` was inside the uploaded archive, containing a live
`SUPABASE_SERVICE_KEY` and `TELEGRAM_BOT_TOKEN`. Your `.gitignore`
correctly excludes it and it is not in the git history — the zip
bypassed that. The service_role key bypasses all RLS, so a leak is a
full database compromise by your own `.gitignore`'s description.

- Supabase → Project Settings → API → service_role → Rotate
- Telegram → BotFather → `/revoke`

The test suite is designed so this cannot happen again through it:
`conftest.py` clears every tuning variable from the environment before
importing `config`, and the `no_network` fixture turns any accidental
outbound HTTP call into a test failure. No suite needs a real key.

---

## 2. Layout

| Path | Cases | Covers |
|---|---:|---|
| `tests/conftest.py` | — | `FakeDB`, synthetic smiles and histories, CQG-shaped XLSX factory |
| `tests/test_smile_math.py` | 47 | `interp`, `moneyness`, `detect_spot`, `atm_iv`, `adjusted_change`, `build_smile` |
| `tests/test_ema.py` | 15 | Cadence independence, missed cycles, half-life semantics |
| `tests/test_detector_pipeline.py` | 28 | `run_for_series` end to end: gating, suppression, evidence assembly |
| `tests/test_chain_and_liquidity.py` | 59 | XLSX parsing, coercion, instrument detection, the liquidity gate |
| `tests/test_ingest.py` | 30 | File stability, quarantine, retire fallbacks, the write path |
| `tests/test_alerts.py` | 29 | Grouping, severity ordering, per-cycle cap, shadow mode |
| `tests/test_db.py` | 25 | Paging, DESC ordering, upsert semantics, series cache |
| `tests/test_expiry_watchdog_config.py` | 47 | Filename and label parsing, staleness transitions, config contracts |
| `tests/js/strategy-engine.test.mjs` | 34 | Payoff, breakevens, max P/L, Black-76, projection |
| `tests/js/strategy-scanner.test.mjs` | 45 | Builders, filters, dedupe, performance |
| `tests/sql/test_schema.sql` | ~40 | Structure, idempotency, partition lifecycle, privileges |

Current state: **280 Python passing, 4 xfailed. 79 JS passing, 6 todo.**
Every xfail and todo is a defect below, each written to assert the
*desired* behaviour so it flips green when fixed.

---

## 3. Defects found

Ordered by what I would fix first.

### 3.1 `findBreakevens` silently returns no breakevens — two separate bugs

**P1. Exact-sample miss.** The sign-change test uses strict comparisons:

```js
if ((y0 < 0 && y1 > 0) || (y0 > 0 && y1 < 0))
```

When a sample lands exactly on the breakeven, `y` is exactly 0 there.
Neither the preceding pair (`y0 < 0, y1 === 0`) nor the following pair
(`y0 === 0, y1 > 0`) is a strict sign change, so the crossing is skipped
and the function returns `[]`.

This is not a floating-point coincidence. It fires whenever
`(breakeven − lo) / step` is an integer — and breakevens are round
numbers (strike + premium) while the plotted range is derived from
strikes. Confirmed failing for a plain long call at 400, 800 and the
2000 default over a 3000–3800 range. **The payoff panel reports no
breakeven for one of the most common structures in the app, with no
error.**

**P1. Epsilon eats crossings at fine resolution.** The grazing guard
scales `eps` to the P&L span but not to the step size:

```js
const eps = Math.max(span * 1e-4, 1e-6);
if (Math.abs(y0) < eps && Math.abs(y1) < eps) continue;
```

As `steps` rises, the P&L change between adjacent samples shrinks, so
eventually both neighbours of a genuine crossing sit inside `eps` and it
is discarded as noise. For a long call over 3005–3800: 5000 steps finds
the breakeven, 20000 returns `[]`.

The two interact badly: the obvious fix for the first (relax to `<=` and
`>=`) makes more crossings land on or near zero, so the second bites
sooner. Fix them together — scale `eps` to the local step, or drop the
eps test entirely and reject flat segments (`y0 === y1`) before testing
for a sign change.

Tests: `strategy-engine.test.mjs` — "a sample landing exactly on the
breakeven", "the exact-sample miss is reproducible", "raising the
resolution must not delete real breakevens".

### 3.2 P1. Iron condor takes ~10 seconds and freezes the tab

Four nested loops over the strike list, with width filters using
`continue` rather than `break`, so iteration is O(puts² × calls²)
regardless of how few combinations survive. Measured **10.2 s** on a
250-strike chain, run synchronously on the main thread from `runScan`.

Fix: precompute each strike's valid width partners once, or sort and
`break` out of the inner loops as the vertical builders already do.

Test: `strategy-scanner.test.mjs` — "iron condor stays responsive".

### 3.3 P1. The default partition is a trap, not a safety net

`011_retention.sql`'s header says that if pg_cron stops, "the system
degrades to *storage grows* instead of *ingest breaks*". True for the
writes, but incomplete.

Postgres cannot create a partition whose range overlaps rows already in
the DEFAULT partition. So once a single day's rows land in
`iv_ticks_default`, `ensure_iv_tick_partitions()` fails for that date
**permanently**, and keeps failing every night until someone empties the
default by hand. Retention can never reclaim those rows either, since
dropping partitions doesn't touch the default.

The real degradation is: storage grows, *and* partition creation is
stuck, *and* retention can't help. That is close to the failure mode
that destroyed the previous database.

Cheapest mitigation is five lines in `watchdog.py`: alert when
`select count(*) from iv_ticks_default` is non-zero. That converts a
silent trap into a Telegram message on day one.

Test: `test_schema.sql` §5.

### 3.4 P1. `latest_chain` has no ordering guard

Upserted on `(series_id, strike)` with nothing comparing `snapshot_ts`.
Replaying a quarantined file — which the README explicitly invites, and
which the `failed/` folder exists to enable — overwrites the current
chain with stale prices. The scanner and payoff builder both read it.

Fix: `... do update ... where latest_chain.snapshot_ts < excluded.snapshot_ts`.

Test: `test_ingest.py::TestLatestChainOrdering` (xfail).

### 3.5 P2. Snapshot timestamps assume the VPS runs UTC

`snapshot_time_from_filename` returns naive and its docstring says the
caller must localize. `ingest_one` does `.replace(tzinfo=utc)` instead,
asserting the filename time was already UTC. CQG names files in browser
local time.

On a VPS behind UTC, snapshots look older than they are and can fall
outside the detector's lookback entirely — silent detection stoppage,
the same failure class `db.py` was written to prevent. On a VPS ahead of
UTC they are stamped in the future, skewing suppression and watchdog
maths.

`test_schema.sql` §9 pins the paired assumption on the database side:
partition bounds use `d::timestamptz`, which resolves against the
session TimeZone.

Fix: convert explicitly with the VPS zone, or force UTC on the VPS and
assert it at startup.

Test: `test_ingest.py::TestTimezoneHandling`.

### 3.6 P2. An unknown rule string crashes a whole detection pass

`_suppression_keys` does
`float(rule.replace("drift_", "").replace("h", ""))`. A `spike_events`
row from an older deploy, or one inserted by hand, raises `ValueError`
and aborts detection for that series — swallowed by `ingest` into a
single log line.

Fix: `try/except` around the parse, skipping unrecognised rules.

Test: `test_detector_pipeline.py` — "unknown rule string in history"
(xfail).

### 3.7 P2. `atm_iv` doesn't filter zero-IV inputs

`build_smile` drops `iv <= 0`; `atm_iv` only drops zeros from the
*result*, so a dead strike quoting `0.0` is fed into the interpolation
and drags the ATM number down. That value is the denominator of
`atm_iv_change_pct` and the subtrahend in every skew calculation, so it
propagates into stored event evidence.

Test: `test_smile_math.py::TestAtmIv` (xfail).

### 3.8 P2. `legOk` fabricates a premium from missing quotes

```js
const p = (px(row, side, "buy") + px(row, side, "sell")) / 2;
if (p == null || p < CFG.min_premium) return false;
```

`null + null === 0` in JavaScript, so `p` is `0`, not `null`, and the
`p == null` guard never fires. A completely unquoted strike is rejected
only incidentally, because `0 < min_premium`. Set `min_premium` to 0 —
which the UI allows — and an unquoted strike passes the liquidity check
and is built into a tradeable-looking spread with a `null` entry price.

Fix: mirror `chain_loader.both_sided` — return false if either side is
null.

Test: `strategy-scanner.test.mjs` — "legOk halves the premium".

### 3.9 P2. Alert ordering `or` bug

`-(g["max_adj"] or g["max_raw"])`. An adjusted change of exactly `0.0`
is falsy, so the group we are *most* confident is smile-roll noise gets
ranked by its large raw change and can push a real event out of a capped
cycle.

Fix: `-(g["max_adj"] if g["max_adj"] is not None else g["max_raw"])`.

Test: `test_alerts.py` — "zero adjustment should not outrank" (xfail).

### 3.10 P2. `isRound` hardcodes gold's grid

`chain_loader.INSTRUMENT_PROFILES` defines `strike_round` per instrument
and its comment says it is "used ONLY by the scanner's optional
round-strikes filter". The scanner ignores it:

```js
const isRound = (s) => Math.abs(s % 25) < 1e-6;
```

On NASDAQ (profile says 50) the filter passes strikes the profile does
not consider round. On SILVER, where strikes are sub-dollar, *every*
strike fails and the filter silently returns nothing.

Test: `strategy-scanner.test.mjs` — "isRound hardcodes a 25-point grid".

### 3.11 P3. Smaller findings, each pinned as a passing test

- **No downside detection.** Both rules use `raw > SPIKE_THRESHOLD_PCT`,
  so a vol *crush* produces no alert. May be intentional; nothing says
  so. `test_detector_pipeline.py::TestDirectionality`.
- **`liquid_strike_count` counts per side**, so it can reach 2× the
  strike count. The README's advice to loosen the gate "if liquid is a
  small fraction of strikes" is calibrated against the wrong
  denominator.
- **`alerted` column is never written** by anything. "Which detections
  actually reached me" will always return nothing.
- **Watchdog `alerted` flag is process-local**, so a restart during an
  outage re-sends, defeating the "two messages, not forty" promise.
- **`_bucket` uses banker's rounding**, so moneyness bands are not
  evenly spaced — 0.005 lands in bucket 0 while 0.015 lands in bucket 2.
- **Liquidity gate fails open** on missing OI or delta, despite the
  docstring saying any one of three checks disqualifies.
- **Delta band assumes CQG's 0–100 scale.** A 0–1 convention would fail
  every strike and silence the monitor without erroring.
- **`COL` has no header validation.** A CQG column reorder parses
  successfully into garbage; the quarantine path only catches files that
  fail to parse. Recommended fix: assert known column names at their
  expected indices on row 4.
- **Futures with `entry_price` 0** report spot as profit — a 69× error
  that looks plausible. Live path is safe, hand-built legs are not.
- **`payoffCurveProjected` defaults `years_to_own_expiry` to 0**, so a
  leg missing that field silently returns the expiry payoff instead of a
  time-value curve. The "what if it's <date>" slider would appear to
  work while showing the wrong thing.
- **Rolling up a dropped partition** writes nothing and raises nothing.
  The 20-minute gap between the 00:20 rollup and 00:40 drop jobs is the
  only thing protecting a day of history, and it is unguarded.

---

## 4. Design notes

**Nothing in the offline suites touches the network.** `db` is replaced
by an in-memory `FakeDB` that records every write, so `on_conflict` and
upsert intent are asserted rather than simulated — the real guarantee
lives in the SQL constraints and is tested in `tests/sql/`.

**`conftest.py` clears tuning variables from the environment** before
importing `config`. Without that, a developer's local `.env` would
change detector thresholds and make tests pass or fail depending on
whose machine they ran on.

**Defect tests assert the desired behaviour, not the current
behaviour**, and are marked `xfail` (Python) or `todo` (Node). Fixing
the code turns them green rather than breaking them. Python xfails are
`strict=False`, so a fix reports as XPASS — remove the marker at that
point.

**Test names are sentences.** When one fails in CI the name alone should
say what broke, without opening the file.

---

## 5. Gaps

Not covered, in rough priority order:

1. **`ingest.run()`'s loop.** The `while True` with `time.sleep` needs
   the loop body extracted into a `run_once()` before it can be tested.
   Worth doing — the loop's exception handling decides whether one bad
   file stops the whole cycle.
2. **`dashboard/app.js` and `strategy-builder.js`.** Both are
   DOM-coupled. Covering them needs jsdom or Playwright, which is a
   bigger dependency decision than the rest of this suite.
3. **End-to-end against a real Supabase.** A scratch project plus a
   fixture file dropped into `WATCH_DIR` would cover the parts the fake
   DB can only assert intent for — particularly PostgREST's actual
   `merge-duplicates` behaviour and the RLS policies under a real
   `authenticated` JWT.
4. **Alert delivery.** `send()` is tested for its guards but never
   against Telegram's real API shape.
5. **Load.** The detector is only exercised with ~30 strikes; a
   250-strike, 3-expiry, 7-day history would show whether
   `run_for_series` stays inside a sensible time budget per ingest.

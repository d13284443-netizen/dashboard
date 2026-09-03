# Vol Desk — dashboard

Read-only web view over everything the IV monitor collects: live chain,
IV history charts, the spike log with raw-vs-adjusted numbers, and
worker health. Plain HTML/JS, no build step — same philosophy as the
worker, so you can edit it by hand.

## What it shows

- **Chain** — current option chain per expiry from `latest_chain`, with
  spot/ATM IV/DTE/liquid stats, the at-the-money row highlighted, and a
  live strip of the last 24h of spikes beside it.
- **IV History** — IV over time for any strike/side, from `iv_ticks`.
- **Spike Log** — 7 days of detections. The **Vol events only** /
  **Would-suppress** filter is the shadow-mode data made visible: it
  splits genuine surface moves from ones the smile filter thinks are
  just spot moving. This is the tab to watch during the test week.
- **System** — worker health and recent log, so anyone can check ingest
  is alive without asking you.

## Setup (5 minutes)

**1. Credentials.** Open `config.js`, fill in two values from Supabase →
Project Settings → API:

- `SUPABASE_URL` — Project URL
- `SUPABASE_ANON_KEY` — the **anon / public** key (NOT service_role)

The anon key is safe in a browser and safe to commit: every row it can
reach is gated by the RLS policies from `schema/010`, which only allow
reads by logged-in users. The service_role key must never go here — it
stays in the worker's `.env` only.

**2. Add your teammates.** In Supabase → Authentication → Users → Add
user, create an account per person with a password. Public sign-ups are
off, so these are the only people who can get in.

**3. Deploy to Vercel.**

```bash
npm i -g vercel      # if you don't have it
cd dashboard
vercel               # first run links the project
vercel --prod        # deploys, gives you a URL
```

Vercel serves the three static files as-is; no config needed. Send the
URL to your team — they sign in with the accounts you made in step 2.

To test locally first without deploying:

```bash
cd dashboard
python -m http.server 8000
# open http://localhost:8000
```

## Notes

- **Read-only by design.** No one can change data from here, including
  you — acknowledging spikes stays in Supabase for now. Loosening that
  later is a small change if you want it.
- **Refresh.** Data updates every 15-20 min on ingest; the page re-polls
  every 60s while its tab is visible, and pauses when hidden.
- **Sessions** persist in the browser, so your team isn't re-logging-in
  constantly. Supabase tokens expire after an hour by default and the
  page bounces to login when that happens; raise it under
  Authentication → Settings if that's annoying.
- **Empty at first.** Charts and chain fill in as the worker collects
  data. A brand-new strike won't have enough history to plot until a few
  snapshots have landed — the chart says so rather than erroring.

## The security model, once

- **anon key** (this dashboard, browsers): public, RLS-gated, read-only.
- **service_role key** (the worker on the VPS): bypasses RLS, read-write,
  never leaves the server.

That split is the whole design. As long as service_role stays out of
anything a browser loads, the worst a leaked anon key allows is a read
that a logged-in user could already do.

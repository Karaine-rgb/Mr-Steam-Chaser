# Sharp Odds

Tracks pre-match soccer 1X2 odds from several bookmakers through
[The Odds API](https://the-odds-api.com) (v4) on its **free plan of 500
credits a month**. You pick a "round" of matches in a Discovery panel; the app
buys odds snapshots for them until kickoff, inside a strict credit budget;
the dashboard compares bookmakers, shows how prices moved, lets you enter
final scores, and exports one CSV per round for offline backtesting.

Repository: `<OWNER>/<REPO_NAME>` (placeholders on purpose: nothing in this
project depends on a particular GitHub account).

> **Read this first.** Snapshots are hours apart far from kickoff and at best
> ~30 min apart just before it. That is set by the credit budget, not by the
> API. Steam labels on such data only describe movement between snapshots,
> and the predecessor tool's backtest found steam gave no reliable edge even
> with 15-minute data. Nothing in this tool has been shown to predict results.

More detail: [`docs/plan.md`](docs/plan.md) (design and credit maths) and
[`docs/odds_api_facts.md`](docs/odds_api_facts.md) (verified API facts).

---

## 1. How the credits are spent

- One refresh of one league costs `markets x ceil(bookmakers / 10)` credits:
  **1 credit** for 1X2 with up to 10 bookmakers, however many matches of that
  league you track. A refresh that returns no matches costs 0.
- Every paid call goes through one gate: budget check, a "pending" ledger row
  written *before* the request, one request (never retried), real cost read
  from the API's `x-requests-last` header. The API's own `x-requests-used`
  counter is the source of truth.
- Automatic polling stops at `min(MONTHLY_CREDIT_LIMIT - CREDIT_RESERVE,
  MONTHLY_CREDIT_LIMIT x CREDIT_PAUSE_PCT %)` = **450** with the defaults.
  Manual "Refresh now" may go up to the pause line, never past it, and always
  shows its cost and asks first.
- Schedules (per kickoff group of a league): Lean 3, Balanced 5 (default),
  Dense 8 snapshots. If a month's plan does not fit, far-from-kickoff
  snapshots are skipped first and the dashboard shows which.
- Mock mode (`ODDS_API_MOCK=1`) uses recorded responses and spends nothing.

## 2. Supabase (database)

Facts checked against Supabase's docs (Nov 2026):

- Free plan: 500 MB database size per project, 2 free projects, and **free
  projects are paused after 1 week of inactivity**. The docs do not say what
  counts as activity; this app queries the database every 30 s while it runs,
  but whether that keeps the project awake is **not verified**. Check the
  project now and then.
- The 500 MB is a billing quota judged on the **average daily size** over the
  billing cycle; going over can restrict the whole organisation (HTTP 402).
  Read-only mode is triggered by **disk** usage (data plus WAL), not by the
  500 MB. The app pauses collection at 95 % of `DB_LIMIT_MB` and the Storage
  window lets you export and delete old rounds. A new project already uses
  about 40-60 MB.
- The free plan's **direct connection is IPv6-only**. Railway's outbound
  IPv6 is off by default. So use the **Session pooler** (IPv4).

Steps:

1. Create a project at supabase.com. Choose a region near Railway's, and keep
   the database password safe (a password manager, not a file in the repo).
2. Click **Connect**, choose **Session pooler**, copy the URI. It looks like
   `postgresql://postgres.<PROJECT-REF>:<PASSWORD>@<POOLER-HOST>:5432/postgres`.
   Copy the host from the dialog; it cannot be built from the region name.
3. Add `?sslmode=require` to the end. That is your `DATABASE_URL`.
4. Nothing else is needed: the app creates its tables at startup
   (`schema.sql`, idempotent). You may also paste `schema.sql` into the SQL
   editor once if you want the tables before the first deploy.

Other connection modes: the **transaction pooler** (port 6543) also works,
because the app turns prepared statements off (`DB_STATEMENT_CACHE_SIZE=0`,
the setting Supabase's docs name for asyncpg) and keeps no session state (its
locks are transaction-scoped, and the poller's "only one copy polls" lease is
a database row). The **direct connection** works only if you enable outbound
IPv6 on the Railway service or buy Supabase's IPv4 add-on.

## 3. Railway (web app + poller)

Facts checked against Railway's and Railpack's docs (Nov 2026): Railpack
builds Python apps from `requirements.txt`; the Python version comes from
`.python-version` (here `3.13`); `railway.json` in the repo sets the start
command, the healthcheck and the shutdown time; Railway sets `PORT`.
During a redeploy the old and new copies overlap briefly; the app is built
for that (see "Restarts" below).

Steps:

1. In Railway: **New project -> Deploy from GitHub repo -> `<OWNER>/<REPO_NAME>`**,
   choose the branch to deploy (auto-deploys on every push to it).
2. Service **Variables** (only the first two are required):

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | the Session pooler URI from step 2 above, with `?sslmode=require` |
   | `ODDS_API_KEY` | your key from the-odds-api.com |
   | `APP_PASSWORD` | **strongly recommended**: without it anyone with the URL can spend credits or delete rounds |
   | `APP_USER` | login name (default `sharp`) |
   | `MONTHLY_CREDIT_LIMIT` | `500` (raise only if you change plan) |
   | `CREDIT_RESERVE` | `50` |
   | `CREDIT_PAUSE_PCT` | `90` |
   | `ODDS_API_MOCK` | `0` in production (`1` = demo data, no credits) |
   | `DB_LIMIT_MB` | `500` |
   | `DB_STATEMENT_CACHE_SIZE` | `0` |

   Never put these values in the repository; `.env.example` lists the names only.
3. **Settings -> Networking -> Generate Domain** to get a public URL.
4. Keep **one replica**. More would only wait on the poller lease.
5. Open the URL, log in, and check:
   - the **credits** pill shows the month's usage; **Re-sync counter (free)**
     reads the API's real counter without spending (it uses the free
     `/sports` call), which also proves the key works;
   - the **DB** pill shows the database size;
   - **Discovery** lists leagues and matches (free calls).
6. Add a round. Within a minute the poller buys the opening snapshot
   (1 credit per league); you can watch it in the credits window.

`Procfile` is included for other hosts; on Railway, `railway.json` wins.

### Restarts and redeploys

Safe at any time:
- Credits: the ledger is in the database. A pending paid call is written
  before it is sent; after a crash it is counted at its expected cost until
  the next API reply settles the real count. No paid call is ever retried
  automatically.
- Two copies at once (deploy overlap): only the holder of the poller lease
  (a row that expires after 90 s) polls, and the credit gate takes a database
  lock, so the budget cannot be overspent.
- Shutdown: `railway.json` gives 40 s to finish (an in-flight API call
  times out after 30 s) and release the lease.
- Round deletes interrupted by a restart resume on startup.

## 4. Running locally

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements-dev.txt
```

```bash
cp .env.example .env
```

Set `DATABASE_URL` in `.env` to any Postgres (a local one, or Supabase), and
`ODDS_API_MOCK=1` to work without a key and without spending credits. Then:

```bash
.venv/bin/uvicorn main:app --reload
```

Open http://localhost:8000.

## 5. Tests

```bash
.venv/bin/python -m pytest -q
```

- Database tests start a throwaway Postgres 16 cluster; they are skipped if
  the Postgres 16 binaries are not installed (`/usr/lib/postgresql/16/bin`).
- The browser test (`tests/ui/discovery_ui.cjs`, run by `tests/test_ui.py`)
  needs Node.js with Playwright and checks every screen at 360, 390, 768,
  1024, 1280 and 1920 px for sideways scrolling. Skip it with `SKIP_UI=1`.
- No test can reach the real Odds API: the client refuses while pytest runs.

## 6. Data and exports

- `odds_snapshots` stores a bookmaker's prices **only when they changed**.
  `match_polls` records every poll per match (time and bookmakers present),
  so "price unchanged" and "bookmaker missing" can be told apart.
- **Download CSV** (per round): every stored snapshot with bookmaker, prices,
  de-vigged fair probabilities (power method), margin columns and final
  scores. The column list is fixed (`app/export.py`, checked by a test).
- **Polls CSV**: the poll log. To rebuild the price at every poll, carry each
  bookmaker's last row forward to each poll where it is listed.
- Downloading is recorded; deleting a round that was never downloaded asks
  for an extra confirmation.

## 7. Moving the repository to another GitHub account

Nothing in the code, config or this README names an account. To move:
1. Transfer the repository on GitHub (or push it to the new account with
   `git remote set-url origin <new URL>` and `git push --all`).
2. In Railway, reconnect the service to the new repository
   (Settings -> Source) and check the branch.
3. Nothing else changes: secrets live in Railway variables, data in Supabase.

## 8. Known limits and unverified points

- Not verified with a live response: soccer quarter-line handicaps, how many
  books price soccer totals/spreads, and the time zone of the API's monthly
  reset (the app counts months in UTC and trusts the API's own counter).
- The "out of credits" error body format is not documented; the app looks
  for the code `OUT_OF_USAGE_CREDITS` anywhere in it.
- Whether this app's activity keeps a free Supabase project from pausing.
- `matchbook` returned no EPL prices in the one live check.
- Mock mode invents prices for leagues without recorded odds; ignore any
  numbers you see in mock mode.
- The engine favourite here is the opening 1X2 favourite, not the
  predecessor tool's Asian-Handicap one, so its old backtest numbers do not
  carry over.

## Credits

Odds data: The Odds API. Charts: TradingView Lightweight Charts™ 4.1.3,
Apache-2.0, served from `frontend/vendor/` with its licence and NOTICE.

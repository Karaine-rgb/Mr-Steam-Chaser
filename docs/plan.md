# Sharp Odds — build plan and credit budget (Phase 1)

Status: **proposal, waiting for approval.** No code has been written yet. No
credit has been spent.

API facts behind every number here: [`odds_api_facts.md`](odds_api_facts.md).

---

## 1. The credit maths

### 1.1 What one refresh costs

One `GET /v4/sports/{league}/odds` call returns **every** event in that league.
So the cost is per **league**, not per match:

```
cost of one refresh of one league = (number of markets) × ceil(number of bookmakers / 10)
```

| Selection | Cost per league refresh |
|---|---|
| h2h, 1–10 bookmakers (**default**) | **1** |
| h2h, 11–20 bookmakers | 2 |
| h2h + totals, ≤10 bookmakers | 2 |
| h2h + spreads + totals, ≤10 bookmakers | 3 |

Two more rules from the docs that save credits:
- A call that returns **no events costs 0**.
- The cost does **not** depend on how many matches are in the league. Five tracked
  matches in the EPL cost the same as one.

Example: think of it like a bus ticket. One ticket (1 credit) carries everyone
going to the same league, whether one match or ten.

### 1.2 The budget

| Item | Default | Meaning |
|---|---|---|
| `MONTHLY_CREDIT_LIMIT` | 500 | Free plan quota (resets on the 1st; time zone not stated, we assume UTC) |
| `CREDIT_RESERVE` | 50 | Never spent automatically |
| `CREDIT_PAUSE_PCT` | 90 | All paid calls stop at 90 % used = 450 |
| **Usable for automatic polling** | **450** | `min(limit − reserve, limit × pause%)` |

With the defaults, the reserve line and the pause line are the same line (450).
So the 50-credit reserve is really a **safety buffer**. It protects against
surprises: the key being used somewhere else, an unknown extra cost such as
`h2h_lay`, or a call whose cost we could not read. That is deliberate. If you
later lower the reserve, the 90 % pause still holds.

### 1.3 A round in numbers

Assumptions (mine, please correct): a Megajackpot round has **17 matches** in
about **8 leagues**. There is about **1 round per week** (≈4.3 per month). Within a
league, matches often split across Saturday and Sunday, so there are about
**1.5 "kickoff groups" per league** (matches less than 2 h apart share a group).
That gives about **12 league-groups per round**.

Each league-group gets snapshots at fixed points before its kickoff. Three
presets:

| Preset | Snapshot times (before kickoff) | Snapshots | Credits / round | Credits / month (4.3 rounds) | Fits in 450? |
|---|---|---|---|---|---|
| Lean | when added, 6 h, 1 h | 3 | 36 | ~155 | yes, lots of room |
| **Balanced (default)** | when added, 24 h, 6 h, 2 h, 30 min | 5 | 60 | **~258** | yes, ~190 left for manual refreshes |
| Dense | when added, 48 h, 24 h, 12 h, 6 h, 3 h, 1 h, 20 min | 8 | 96 | ~413 | just, almost nothing left |

These are worst cases. When groups in one league are close together, a single call
serves both, so real usage should be a bit lower.

What doubles the cost: adding `totals` (×2), or choosing 11–20 bookmakers (×2).
**Balanced + totals ≈ 516/month, which does not fit.** So extra markets stay
off unless you turn them on for one round. The cost estimate will show this
before you confirm.

If you track 2 rounds a week, Balanced becomes ~516/month, which does not
fit. The planner would then thin the schedule automatically (see 2.3), and
the estimate shows it in advance.

### 1.4 What this means for the data (honest part)

- The old tool snapshotted every 30–120 seconds. This one gets **3–8 snapshots per
  match in total**. Snapshot gaps are **hours** far from kickoff and at best
  **~30 min** just before kickoff.
- The API itself refreshes odds about every 60 s (exchanges every 20 s). So
  the coarseness comes from **our budget, not from the API**.
- Steam labels on this data only describe "the price moved between snapshot A
  and snapshot B". They cannot tell a sudden move from a slow drift inside that
  gap. The old backtest found steam had no reliable edge even at 15-minute data.
  The steam panel will say this in plain words and show the real gap.

---

## 2. How the budget manager works (Phase 2, built first)

### 2.1 One gate for every paid call

`app/credits.py :: paid_call(endpoint, params, expected_cost, reason)` is the only
code path allowed to call a paid endpoint:

1. **Check.** Refuse if `used + expected_cost` passes the pause line, or the daily
   soft cap (unless it is a manual refresh you confirmed). Refuse when paused.
2. **Write intent.** Insert a ledger row with status `pending` *before* the
   HTTP request, inside the same DB transaction that takes a Postgres advisory
   lock. This means two app copies (for example, during a Railway redeploy) can never
   both spend.
3. **Call once.** No automatic retry of a paid call. The key goes in the
   `x-api-key` header, never in the URL.
4. **Record.** Update the row with `x-requests-last` (real cost),
   `x-requests-used`, `x-requests-remaining`, HTTP status and event count. Status
   becomes `ok` / `error` / `empty`.
5. **Unknown outcome.** If there is a timeout or crash after sending, the row stays
   `pending`. At startup or the next tick, pending rows older than 5 min are
   marked `unknown` and counted as `expected_cost` until re-synced. Re-sync uses the
   **free** `/sports` call, which returns the real `x-requests-used`. So a crash can
   cost at most one call and never causes a blind double call.

The API's `x-requests-used` is the source of truth. The ledger is our record
of *why* each credit was spent.

### 2.2 Daily soft cap

`soft cap today = credits left before pause ÷ days left in the month (UTC)`.
Rounds are bursty: a weekend needs much more than a Tuesday. So the cap
**throttles only extra polls**. Snapshots already in the plan for tracked matches
are budgeted for the whole month in advance (2.3). The widget shows the cap and
today's spend.

### 2.3 Poll planner

- It builds the list of **planned snapshots** for every tracked match from now to
  the end of the month, then merges them per league (one call can serve several
  matches).
- If the planned total is more than the credits available, it drops snapshots in a
  fixed order until the plan fits. It keeps "when added" and the last one
  before kickoff for as long as possible, and drops middle ones first. The
  dashboard shows which snapshots were dropped.
- On each tick (every 60 s, cheap DB check only), it calls the league whose next
  planned time has passed, preferring nearest kickoff and then longest time since
  its last snapshot.
- Each call uses `eventIds` (only tracked matches) and `commenceTimeFrom=now`. This
  never pulls live games, and if all tracked matches already started, the call
  returns nothing and costs 0. It never polls after kickoff. A match is
  unmonitored at kickoff, as in the old tool.
- Manual "Refresh now" shows its exact cost and asks to confirm. It uses the same gate.

### 2.4 Pause and banner

At the pause line, all paid calls stop and a red banner explains why. It resumes
automatically when the API's `x-requests-used` drops (monthly reset) or when you
raise `MONTHLY_CREDIT_LIMIT`.

### 2.5 Mock mode

`ODDS_API_MOCK=1` serves recorded JSON fixtures from `tests/fixtures/` for
`/sports`, `/events` and `/odds`, with fake usage headers. The ledger still records
"mock" calls, flagged so they never count against the real budget. **All tests
run in mock mode and the HTTP client refuses to start a real request while
`PYTEST_CURRENT_TEST` is set.**

Fixtures will first be **hand-written from the documented schema** and labelled
as such. After you approve a live check (section 5), they will be replaced with
real recorded responses (key stripped).

---

## 3. Data model (Supabase free, 500 MB)

| Table | Key columns |
|---|---|
| `matchups` | `id`, `event_id` (API id, unique), `sport_key`, `league_title`, `home_team`, `away_team`, `commence_time`, `round_label`, `is_monitored`, `home_score`, `away_score`, `added_at` |
| `round_settings` | `round_label` PK, `bookmakers text[]`, `markets text[]`, `schedule_preset` |
| `odds_snapshots` | `id`, `matchup_id`, `bookmaker` (key), `market` (h2h/spreads/totals), `captured_at`, `market_last_update`, `home_price`, `draw_price`, `away_price` (over = home slot, under = away slot, as in the old export), `point`, `fair_home`, `fair_draw`, `fair_away`, `ledger_id` |
| `credit_ledger` | `id`, `created_at`, `endpoint`, `sport_key`, `markets`, `regions_or_books`, `expected_cost`, `charged` (x-requests-last), `used_after`, `remaining_after`, `http_status`, `events_returned`, `status` (pending/ok/empty/error/unknown/mock), `reason` |
| `round_downloads` | as in the old tool |
| `app_settings` | key/value (non-secret only, never the API key) |

- **Dedupe:** a row is written only when that bookmaker's prices or `point` for that
  market changed since its last stored row (version-diff on the prices). No `raw_json`
  column, because the old tool's raw JSON was most of its size.
- Margin columns are **computed at export**, using the old formulas
  (`margin_columns`), so the stored rows stay narrow.
- **Size estimate:** 17 matches × 10 books × ≤8 snapshots ≈ 1,400 rows per round
  at most, about 0.3 MB with indexes. Storage is not the bottleneck here, credits
  are. The storage guard (pause at 95 %, resume 2 points lower) is still copied
  over.
- `ensure_schema()` at startup is idempotent. `schema.sql` is the same DDL for a first manual run.
- Supabase pooler: `DB_STATEMENT_CACHE_SIZE` setting, **default 0**. It is safe for both
  direct and pooled URLs at a small speed cost. Still to verify against Supabase
  docs, which are blocked from here (see facts note).

---

## 4. Phases (I stop for your OK after each)

| # | Phase | Main output | Tests |
|---|---|---|---|
| 1 | API facts + plan + credit maths | `docs/odds_api_facts.md`, this file | — |
| 2 | Budget manager + mock mode | `app/config.py`, `app/db.py` (+schema), `app/odds_api.py` (client, mock), `app/credits.py` (gate, ledger, reconcile), `app/planner.py` (cost estimator, schedule, thinning), `.env.example`, `.gitignore`, `requirements.txt` | credit accounting, pending/unknown recovery, pause threshold, reserve, soft cap, planner under tight / normal / huge budgets, cost estimator, parser (draws, quarter lines, missing books, empty events), "tests can't call real API" guard |
| 3 | Discovery panel | `/api/discovery/*` (cached free calls), league browser (country groups, search, popular), match list with filters, bookmaker/market pickers, **live cost estimate** | cache prevents repeat calls, estimate maths; Playwright at 360/768/1280/1920 px in mock mode |
| 4 | Snapshots + dashboard | poller (in web process, advisory-locked), ingest with dedupe, round selector, summary table, drill-down charts (lightweight-charts), results entry that keeps unsaved scores, credit widget, storage modal, polling updates via diff rendering | snapshot dedupe, kickoff cutoff, storage guard, round delete, UI widths |
| 5 | Analysis + export + scorecard | per-match book comparison (best price, margin per book, off-market spread vs reference), Pinnacle-as-reference or consensus (labelled), steam per bookmaker + "confirmed in >1 book", coarse-data note, scorecard, CSV export | de-vig/margin, steam on coarse data (short windows skipped), multi-book confirmation, export columns stable |
| 6 | Railway + Supabase guide | `Procfile`, README with `<OWNER>/<REPO_NAME>` placeholders | fresh-start smoke test in mock mode |

### What I will reuse from Sharp Karaine (copied and adapted, never imported)

`odds.py` (power de-vig, margin columns), `signals.py` (move spans, steam,
early/late, favourite), `scorecard.py`, `storage_guard.py`, `round_delete.py`,
the CSV export with margin columns, and the frontend look, layout and diff
rendering.

**Not copied:** the old `config.py` contains a hard-coded Arcadia guest key as
a default value. Nothing like that will exist here. The new config has no secret
defaults.

### Changes from the old analysis that you should know about

- The old "engine favourite" uses the **opening Asian Handicap** favourite. With
  h2h only there is no AH line. The new engine favourite is the **opening h2h
  fair favourite** of the reference book. That is a different rule, and the old
  backtest numbers do not carry over to it. The scorecard will say so.
- The old steam window has a 15-min minimum (`velocity_min_window_hours`). Here
  the minimum becomes the **actual gap between the two snapshots**. Windows shorter
  than that are skipped, not faked.
- Exchanges (Betfair, Matchbook, Smarkets) quote prices without their
  commission, so their "margin" can be near 0 or even negative. The power de-vig
  handles that (the old code already does). The UI will mark exchange margins as
  "before commission".

---

## 5. Decisions I need from you

1. **Repository.** This session is attached to `Karaine-rgb/Mr-Steam-Chaser`
   (empty, no commits). Is that the new repo for Sharp Odds?
2. **Branch.** The session is set to develop on `claude/sharp-odds-setup-6wd9ag`.
   The repo has no `main` yet. Pushing a feature branch first would make *it*
   the default branch. May I push one initial commit (`.gitignore`, `README`
   stub, these docs) to `main`, then work on the feature branch?
3. **Budget defaults.** Balanced preset, h2h only, ≤10 bookmakers, reserve 50,
   pause 90 %. OK?
4. **Default bookmakers (≤10 = 1 credit).** Agreed list, with `sport888` removed:
   `pinnacle`, `betfair_ex_eu`, `matchbook`, `smarkets`, `onexbet`, `marathonbet`,
   `williamhill`, `unibet_uk`, `betway` (9). bet365 was requested but is not
   available on the free plan (only `bet365_au`, paid, AFL/NRL). `matchbook`
   returned no EPL prices in the live check. Both are open questions.
5. **Live check.** To replace guesses with real fixtures I need: (a) free calls to
   `/sports` and `/events` (0 credits), and (b) **one** `/odds` call for one soccer
   league, h2h, the 10 books above (**1 credit**). This session can't reach
   `api.the-odds-api.com` (blocked), and your key must not come into chat. Two
   safe options:
   - **A.** You run a small script I write (`scripts/record_fixtures.py`, reads
     `ODDS_API_KEY` from your env and sends it in a header, so the saved JSON files
     contain no key) and upload the files it saves.
   - **B.** I ask for the key through the secure credential card. It is stored on
     the environment and injected by the network proxy, so I never see it and it never
     reaches the repo. This differs from your "env var only" rule, so I won't do it
     unless you say so.
   - Or skip it for now: everything is built against the documented schema, and
     the gaps stay marked "unverified".

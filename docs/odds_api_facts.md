# API facts I verified — The Odds API v4

Read on 2026-11-12 from the public documentation pages listed at the bottom.
**No live API call was made** (no key was used, no credit was spent). Everything
below comes from the docs as written on that date. Points the docs do not
answer are marked **NOT STATED**. Those must be checked with a real response
before the code depends on them.

Legend: ✅ your belief was right · ⚠️ right, but there is more to it · ❌ wrong · ❓ the docs do not say

## 1. Host and authentication

| Your belief | Verdict | What the docs say |
|---|---|---|
| Base URL `https://api.the-odds-api.com/v4` | ✅ | Host `https://api.the-odds-api.com`, paths start with `/v4/...`. An IPv6 host also exists: `https://ipv6-api.the-odds-api.com`. |
| Auth by `apiKey` query parameter | ⚠️ | Both ways work: the `apiKey` query parameter, **or** the `x-api-key` request header ("for extra privacy"). **Plan: use the header**, so the key never shows up in URLs, access logs or error messages. |

## 2. Endpoints and credit cost

| Endpoint | Your belief | Verdict | Docs |
|---|---|---|---|
| `GET /v4/sports` | free | ✅ | "does not count against the usage quota". Optional `all=true` also lists out-of-season sports. Fields: `key`, `group`, `title`, `description`, `active`, `has_outrights`. **It still returns the usage headers**, so this free call can re-sync our credit counter with the real one at no cost. |
| `GET /v4/sports/{sport}/events` | free | ✅ | "does not count against the usage quota". Returns `id`, `sport_key`, `sport_title`, `commence_time`, `home_team`, `away_team`; no odds. Filters: `eventIds`, `commenceTimeFrom`, `commenceTimeTo`, `dateFormat`. Includes in-play events. |
| `GET /v4/sports/{sport}/odds` | all events of the league in one call; cost = markets × regions | ⚠️ | Right. It returns **upcoming and live** games ("only returns events for which odds are available"). Cost = `[markets specified] × [regions specified]`. **"If no events are returned, the request will not count against the usage quota."** Note: the cost counts markets *specified*, not markets returned, so asking for `spreads` costs a credit even if no bookmaker prices it (as long as at least one event comes back). |
| `bookmakers` instead of `regions` | each group of 10 = 1 region | ✅ | "Every group of 10 bookmakers is the equivalent of 1 region … up to 10 bookmakers counts as 1 region … between 11 and 20 … 2 regions." Bookmakers may come from any region. If both are given, `bookmakers` wins. |
| `GET /v4/sports/{sport}/events/{eventId}/odds` | one event, extra markets | ⚠️ | Right, and **the cost rule is different**: `[unique markets returned] × [regions]`. Empty responses cost 0. Accepts every market key (the `/odds` endpoint accepts only featured markets: h2h, spreads, totals, outrights; others give `INVALID_MARKET`). |
| `GET /v4/sports/{sport}/scores` | costs credits | ✅ | 1 credit; **2 credits** with `daysFrom` (1–3 days back). Only "selected sports". We will not use it by default: you enter scores by hand. |
| Historical odds | paid plans only | ✅ | "only available on paid usage plans" (error `HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN`). The pricing page shows "Historical Odds" crossed out on the free plan. Cost on paid plans would be 10 × markets × regions. |
| *(not in your list)* `GET /v4/sports/{sport}/events/{eventId}/markets` | — | new | Lists the market keys each bookmaker has for one event. **Costs 1 credit.** |
| *(not in your list)* `GET /v4/sports/{sport}/participants` | — | new | Lists teams. **Costs 1 credit.** Not needed. |

Useful `/odds` parameters: `regions`, `bookmakers`, `markets`, `oddsFormat`
(default **decimal**), `dateFormat` (default iso), `eventIds`,
`commenceTimeFrom`, `commenceTimeTo`, `includeLinks`, `includeSids`,
`includeBetLimits`, `includeRotationNumbers`.

## 3. Usage headers

✅ `x-requests-remaining`, `x-requests-used`, `x-requests-last` are on every
response (integers in the spec). `x-requests-used` is "credits used since the
last quota reset". So **the API's own counter is the source of truth**, and our
ledger is reconciled against it on every response.

## 4. Markets and response shape (soccer)

| Your belief | Verdict | Docs |
|---|---|---|
| `h2h` is 3-way for soccer with a Draw | ✅ | "includes the draw for soccer". Outcome `name` is the team name, or `"Draw"`. A separate `h2h_3_way` key exists, but only on the per-event endpoint. We do not need it. |
| `spreads`, `totals` | ⚠️ | They exist, but the docs say: **"spreads and totals markets are mainly available for US sports and bookmakers at this time."** For soccer at EU/UK books they may be thin or missing. Because `/odds` charges per market *specified*, opting into them may cost credits for little data. ❓ Real soccer coverage: NOT STATED; check on a live response. |
| Quarter lines (e.g. −0.25, 2.25) | ❓ | `point` is a plain number ("the handicap or points of the outcome"). Whether soccer spreads use quarter lines is **NOT STATED**. The parser will accept any decimal `point` and never assume half lines. |
| Decimal odds | ✅ | Default `oddsFormat` is decimal. |

Response shape for `/odds` (from the OpenAPI spec):
event `{id (32 chars), sport_key, sport_title, commence_time, home_team, away_team, bookmakers[]}` →
bookmaker `{key, title, last_update, markets[]}` →
market `{key, last_update, outcomes[]}` →
outcome `{name, price, point?}`.

- ⚠️ **The bookmaker-level `last_update` is deprecated.** The docs say to use the
  **market-level `last_update`**. We will store the market-level one.
- A market that is suspended or closed stops updating and "is removed from the
  API response after about 15 minutes". So a bookmaker or market can vanish
  between snapshots. The parser must treat that as "no data", not as an error.
- The spec says "allow for the addition of new market types". The parser
  ignores unknown market keys.
- Exchanges: `h2h_lay` (lay odds) is "automatically included with h2h
  results" for Betfair, Matchbook, etc. ❓ **Whether `h2h_lay` adds to the
  cost is NOT STATED.** The ledger will show it on the first real call
  (`x-requests-last`). We will store back prices only. Lay prices are skipped.

## 5. Bookmakers: Pinnacle and exchanges

| Bookmaker | Key | Region(s) | Note in docs |
|---|---|---|---|
| Pinnacle | `pinnacle` | **eu** | "Odds are from public website which may incur a delay". Not marked paid-only. |
| Betfair Exchange | `betfair_ex_uk` / `betfair_ex_eu` / `betfair_ex_au` | uk / eu / au | — |
| Matchbook (exchange) | `matchbook` | uk, eu | — |
| Smarkets (exchange) | `smarkets` | uk | — |

- Free plan = "Most bookmakers". The docs mark these as **paid only**:
  `williamhill_us`, `fanatics`, `courtside`, `rebet`, `bet365_au`, `dabble_au`,
  `bet99_ca_on`. Pinnacle and the exchanges are **not** on that list, so they
  appear to be in the free plan. ❓ This is not verified with a live free key.
- Regions in the docs: `us`, `us2`, `us_dfs`, `us_ex`, `uk`, `eu`, `au`, `ca`,
  `fr`, `se`, `fi`.
- ⚠️ **Pinnacle's odds here come from its public website "which may incur a
  delay".** That is a different feed from the Arcadia API the old tool used.
  Pinnacle numbers from the two tools are not directly comparable.

## 6. Free plan, reset, rate limit, errors

| Your belief | Verdict | Docs |
|---|---|---|
| 500 credits / month | ✅ | Starter plan: free, 500 credits per month, all sports, most bookmakers, all betting markets, no historical odds. |
| Monthly reset rule | ⚠️ | FAQ: **"Usage credits are automatically reset on the first of every month."** ❓ **The time zone is NOT STATED.** The app will assume UTC and also trust the API's own `x-requests-used` (if it drops to near 0, a reset happened). |
| Rate limit | ⚠️ | **30 requests per second**, answered with HTTP **429** (`EXCEEDED_FREQ_LIMIT`). The docs suggest retrying after a couple of seconds. We will not auto-retry paid calls (see plan). |
| Out of credits | new | Error code `OUT_OF_USAGE_CREDITS`. The OpenAPI spec says HTTP **401** covers both a bad key and a key "at its usage limit". The body says which one. |
| Data freshness | new | Featured markets (h2h/spreads/totals) update about every **60 s** before kickoff. Exchanges about every **20 s**. The interval shortens over the last 6 h before kickoff. So the feed is fresh. **Our snapshot gap (hours) is set by our credit budget, not by the API.** |
| Expired event | new | Per-event endpoints return HTTP **404** (`EVENT_NOT_FOUND`) once an event has finished. |

## 7. Soccer league keys

`/sports` gives a flat list. The `group` for soccer is "Soccer". **There is
no country field.** The docs' sports list shows keys like `soccer_epl`,
`soccer_germany_bundesliga`, `soccer_spain_la_liga`, `soccer_efl_champ`,
`soccer_uefa_champs_league`, `soccer_usa_mls` (about 70 soccer keys). Country grouping in the
Discovery panel will be **derived** from the key/title (for example
`soccer_germany_*` → Germany). That is our heuristic, not an API field, and it
will be labelled as such. Leagues it cannot place go under "Other".

❓ Not checked: whether the leagues in a typical SportPesa Megajackpot (often
lower divisions and smaller countries) are covered. Discovery will show it.

## 8. Still unverified (needs one live response)

1. Real soccer `title`/`description` text from `/sports` (free call).
2. Whether a **free** key actually returns `pinnacle` and `betfair_ex_*` prices for soccer.
3. Whether `h2h_lay` adds cost.
4. Whether soccer `spreads` use quarter lines, and how many EU/UK books price spreads/totals for soccer.
5. Time zone of the monthly reset.

Items 1 and part of 5 need only free calls (`/sports`, `/events`). Items 2–4
need one paid `/odds` call (**1 credit** for h2h with ≤10 bookmakers). I will
not make it without your approval.

## Sources read (2026-11-12)

- https://the-odds-api.com/liveapi/guides/v4/
- https://the-odds-api.com/api-reference/v4/schema.yml (OpenAPI spec)
- https://the-odds-api.com/sports-odds-data/bookmaker-apis.html
- https://the-odds-api.com/sports-odds-data/betting-markets.html
- https://the-odds-api.com/sports-odds-data/sports-apis.html
- https://the-odds-api.com/sports-odds-data/update-intervals.html
- https://the-odds-api.com/liveapi/guides/v4/api-error-codes.html
- https://the-odds-api.com/manage/faqs.html
- https://the-odds-api.com/manage/upgrade-downgrade-cancel-a-subscription.html
- https://the-odds-api.com/ (pricing cards)

## Related: Supabase / asyncpg (not Odds API)

- `supabase.com` is **blocked** from this build environment, so the Supabase
  pooler advice is **not verified** yet.
- What I could verify: asyncpg's own source code (v0.31.0, `connection.py`)
  documents `statement_cache_size=0` as "disable the cache" and comments that
  this is the setting for people "running PgBouncer". The app will expose it as
  `DB_STATEMENT_CACHE_SIZE` (default 0 when the URL looks like a pooler, else 100).
  It will be checked against Supabase's docs in Phase 4.

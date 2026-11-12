# Recorded live responses

Real responses from The Odds API v4, recorded on 2026-11-12 around 09:43 UTC.
The API key was sent in the `x-api-key` header (injected by a network proxy), so
it does not appear in any file here. Only the body is stored. The usage headers
are listed below.

| File | Request | Credits charged (`x-requests-last`) | `x-requests-used` after |
|---|---|---|---|
| `sports_all.json` | `GET /v4/sports/?all=true` | 0 | 0 |
| `events_soccer_epl.json` | `GET /v4/sports/soccer_epl/events` | 0 | 0 |
| `events_soccer_germany_bundesliga.json` | `GET /v4/sports/soccer_germany_bundesliga/events` | 0 | 0 |
| `odds_soccer_epl_h2h.json` | `GET /v4/sports/soccer_epl/odds?markets=h2h&oddsFormat=decimal&bookmakers=pinnacle,betfair_ex_eu,matchbook,smarkets,onexbet,marathonbet,williamhill,unibet_uk,betway` | 1 | 1 |

Do not edit these files. They power mock mode (ODDS_API_MOCK=1) and the tests.
Cases not seen live (quarter lines, missing books, empty events) are
hand-written inside the tests and labelled as such.

## Second recording (Phase 3)

`events_<league>.json` for every soccer league marked active in
`sports_all.json` (45 leagues, 452 events), recorded on 2026-11-12 with
`GET /v4/sports/<league>/events`. Every call returned `x-requests-last: 0`
and `x-requests-used` stayed at 1, so this cost nothing. The EPL and
Bundesliga files from the first recording were kept.

`recordings.json` lists when each file was recorded. Mock mode shifts each
file's timestamps by its own recording time.

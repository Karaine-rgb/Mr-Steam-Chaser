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

Do not edit these files. Tests that need other cases (quarter lines, missing
books, empty events) use hand-written fixtures in a separate folder, labelled
as hand-written.

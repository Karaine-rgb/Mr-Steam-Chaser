# Deployment facts I verified (Supabase, Railway, Railpack)

Read on 2026-11-12 from the pages listed at the bottom. Quotes are short;
anything not stated there is marked NOT STATED.

## Supabase

| Topic | What the docs say | What the app does |
|---|---|---|
| Free plan size | "500 MB database size" per project; 2 free projects | Storage guard pauses collection at 95 % of `DB_LIMIT_MB` (500) |
| What 500 MB means | A billing quota per organisation, judged on the **average daily** database size over the billing period; above it the org can be restricted (requests return **402**) | Same guard; Storage window to export and delete rounds |
| Read-only mode | "Read-only mode depends on disk usage. The 500 MB database size quota does not trigger it." Back to read-write below 95 % of the disk size; WAL also uses disk ("On a 2 GB disk, WAL can fill the disk") | Writes are small and batched per poll |
| New project size | "~40-60mb" before any data | Counted by the guard (it measures the whole database) |
| Pausing | "Free projects are paused after 1 week of inactivity." What counts as activity: NOT STATED | Flagged in README as unverified |
| Direct connection | IPv6 on the free plan (IPv4 only with the paid add-on) | README recommends the Session pooler |
| Session pooler | `postgres.<PROJECT-REF>` user, pooler host, port 5432, IPv4 on every plan; supports prepared statements and session state | Recommended `DATABASE_URL` |
| Transaction pooler | Port 6543; no prepared statements; session-level advisory locks, LISTEN, temp tables and pipelining do not survive | Works too: `DB_STATEMENT_CACHE_SIZE=0` (asyncpg's documented fix), only transaction-scoped locks, poller lease is a row |
| asyncpg | "disable automatic use of prepared statements by passing statement_cache_size=0" | Default `0` |
| SSL | "Set SSL to require" | README: add `?sslmode=require` (asyncpg parses `sslmode` from the URL; checked in its source) |

## Railway

| Topic | What the docs say | What the app does |
|---|---|---|
| Port | "Railway will inject a PORT environment variable that your application should listen on." | Start command uses `${PORT:-8000}` |
| Start command | Railpack detects Procfiles, "although this is not recommended"; setting the start command in settings or `railway.json` is preferred | `railway.json` sets it; `Procfile` kept for other hosts |
| Config as code | `railway.json` with `deploy.startCommand`, `healthcheckPath`, `healthcheckTimeout`, `drainingSeconds`, `overlapSeconds`, `restartPolicyType` | Used |
| Healthcheck | New deployment becomes active only after a 2xx from the path; not monitored afterwards | `/api/health` checks the database |
| Overlap and shutdown | Old and new deployments overlap briefly; the old one gets SIGTERM, then SIGKILL after the draining time (default 0 s) | `drainingSeconds: 40`; poller lease and credit gate handle the overlap |
| Outbound IPv6 | Opt-in per service, disabled by default | Why the Session pooler is recommended |

## Railpack (Railway's builder)

- Python version order: `RAILPACK_PYTHON_VERSION`, then `.python-version`
  (mise-compatible files), then `runtime.txt`, else default 3.13.2. The repo
  pins `3.13` (all tests ran on Python 3.13).
- FastAPI detected when `fastapi` is a dependency; with uvicorn present its
  default start is `uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}`,
  the same command `railway.json` sets explicitly.
- Dependencies from `requirements.txt` (pip).

## Not verified

- Whether the app's own database traffic keeps a free Supabase project from
  pausing.
- A real deploy: nothing was deployed from here (no Railway or Supabase
  account access in this environment). The fresh-start test below ran
  locally with the exact start command and a fresh install of
  `requirements.txt`.

## Sources

- https://supabase.com/docs/guides/database/connecting-to-postgres
- https://supabase.com/docs/guides/troubleshooting/disabling-prepared-statements-qL8lEL
- https://supabase.com/docs/guides/platform/database-size
- https://supabase.com/pricing
- https://supabase.com/docs/guides/platform/billing-on-supabase
- https://docs.railway.com/reference/variables
- https://docs.railway.com/guides/build-configuration
- https://docs.railway.com/reference/config-as-code
- https://docs.railway.com/reference/healthchecks
- https://docs.railway.com/reference/deployments
- https://docs.railway.com/guides/deployment-teardown
- https://docs.railway.com/reference/outbound-networking
- https://railpack.com/languages/python

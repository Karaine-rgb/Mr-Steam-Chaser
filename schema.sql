-- Sharp Odds schema. Idempotent: safe to run again. The app also runs this
-- file at startup (app/db.py ensure_schema), so a manual run in the Supabase
-- SQL editor is only needed if you want the tables before the first deploy.

create table if not exists matchups (
    id              bigserial primary key,
    event_id        text unique not null,      -- The Odds API event id (32 chars)
    sport_key       text not null,             -- e.g. soccer_epl
    league_title    text,
    home_team       text not null,
    away_team       text not null,
    commence_time   timestamptz not null,
    round_label     text,
    is_monitored    boolean not null default false,
    home_score      integer,                   -- entered by hand after the match
    away_score      integer,
    added_at        timestamptz not null default now()
);
create index if not exists idx_matchups_round on matchups (round_label);
create index if not exists idx_matchups_monitored on matchups (is_monitored, sport_key, commence_time);

-- Per-round choices: which bookmakers and markets to pay for, which schedule.
create table if not exists round_settings (
    round_label      text primary key,
    bookmakers       text[] not null,
    markets          text[] not null default '{h2h}',
    schedule_preset  text not null default 'balanced',
    created_at       timestamptz not null default now()
);

-- Every API call, free or paid. Paid calls get a 'pending' row BEFORE the
-- request is sent, so a crash can never hide a spend.
create table if not exists credit_ledger (
    id                bigserial primary key,
    created_at        timestamptz not null default now(),
    finished_at       timestamptz,
    endpoint          text not null,            -- sports | events | odds
    is_paid           boolean not null,
    is_mock           boolean not null default false,
    kind              text,                     -- planned | extra | manual | sync | discovery
    sport_key         text,
    markets           text,
    bookmakers        text,
    events_requested  integer,
    expected_cost     integer not null default 0,
    charged           integer,                  -- x-requests-last; null = not known
    used_after        integer,                  -- x-requests-used
    remaining_after   integer,                  -- x-requests-remaining
    http_status       integer,
    events_returned   integer,
    error_code        text,
    status            text not null,            -- pending | ok | empty | error | not_sent | unknown
    reason            text
);
create index if not exists idx_ledger_created on credit_ledger (created_at desc);
create index if not exists idx_ledger_status on credit_ledger (status) where status in ('pending', 'unknown');

-- Narrow snapshot rows; a row is written only when a bookmaker's prices
-- changed. Margin columns are computed at export time, not stored.
create table if not exists odds_snapshots (
    id                  bigserial primary key,
    matchup_id          bigint not null references matchups(id) on delete cascade,
    bookmaker           text not null,
    market              text not null,           -- h2h | spreads | totals
    captured_at         timestamptz not null,
    market_last_update  timestamptz,
    home_price          double precision,        -- totals: Over
    draw_price          double precision,
    away_price          double precision,        -- totals: Under
    point               double precision,        -- spreads: home handicap; totals: line
    fair_home           double precision,
    fair_draw           double precision,
    fair_away           double precision,
    ledger_id           bigint references credit_ledger(id) on delete set null
);
create index if not exists idx_snapshots_match on odds_snapshots (matchup_id, bookmaker, market, captured_at);

create table if not exists round_downloads (
    round_label     text primary key,
    downloaded_at   timestamptz not null default now(),
    snapshot_count  bigint not null default 0,
    download_count  integer not null default 1
);

-- Non-secret settings only. The API key is never stored here.
create table if not exists app_settings (
    key    text primary key,
    value  text
);

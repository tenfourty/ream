-- artifacts on SQLite: the final shape of schema.sql, as one fresh schema
-- (docs/agent/specs/2026-10-09-sqlite-design.md).
--
-- Declared types carry meaning here, unlike in plain SQLite: the connection
-- opens with detect_types, so a TIMESTAMPTZ column comes back as an aware
-- datetime, BOOLEAN as a bool and JSONB as a dict or list, as they do from
-- Postgres. Timestamps are stored as text in one format,
-- 'YYYY-MM-DD HH:MM:SS.ffffff+00:00', so comparing and ordering them as text
-- is correct. A default time comes from artifacts_now(), which the connection
-- registers and which writes that same format (SQLite's own %f has three
-- digits, which would break the ordering).
--
-- db.py runs this file as step 1, statement by statement, inside one
-- BEGIN IMMEDIATE, and sets PRAGMA user_version = 1. A later change is a new
-- numbered step, never an edit to this file.

create table agent_key (
    id             text primary key,
    agent          text not null,
    token_sha256   text not null unique,
    created_at     timestamptz not null default (artifacts_now()),
    revoked_at     timestamptz,
    label          text,
    last_used_at   timestamptz,
    rotating       boolean not null default 0,
    replaces       text,
    first_used_at  timestamptz,
    revoked_reason text
);

create table artifact (
    id              text primary key,
    title           text not null,
    agent           text not null,
    visibility      text not null default 'private'
                    check (visibility in ('private', 'link', 'public')),
    read_token      text not null,
    current_version integer,
    created_at      timestamptz not null default (artifacts_now()),
    updated_at      timestamptz not null default (artifacts_now()),
    archived_at     timestamptz,
    pinned_at       timestamptz,
    deleted_at      timestamptz,
    session         text,
    agents_can_read boolean not null default 1,
    foreign key (id, current_version) references version (artifact_id, n)
        on delete set null
);
create index artifact_agent_idx on artifact (agent);
create index artifact_live_idx on artifact (deleted_at) where deleted_at is null;

create table version (
    artifact_id   text not null references artifact (id) on delete cascade,
    n             integer not null,
    body          text not null,
    revision      text not null,
    status        text not null default 'live' check (status in ('live', 'pending')),
    created_by    text not null,
    created_at    timestamptz not null default (artifacts_now()),
    thumbnail     blob,
    session       text,
    render_errors jsonb check (render_errors is null or json_valid(render_errors)),
    rendered_at   timestamptz,
    note          text,
    section       text,
    notify        boolean not null default 0,
    primary key (artifact_id, n)
);

create table file (
    artifact_id  text not null,
    n            integer not null,
    path         text not null,
    content_type text not null,
    bytes        blob not null,
    primary key (artifact_id, n, path),
    foreign key (artifact_id, n) references version (artifact_id, n) on delete cascade
);

create table state (
    artifact_id text not null references artifact (id) on delete cascade,
    path        text not null,
    doc         jsonb not null check (json_valid(doc)),
    updated_at  timestamptz not null default (artifacts_now()),
    primary key (artifact_id, path)
);

create table artifact_share (
    artifact_id text primary key references artifact (id) on delete cascade,
    token       text not null unique,
    created_at  timestamptz not null default (artifacts_now())
);

create table comment (
    id              text primary key,
    artifact_id     text not null references artifact (id) on delete cascade,
    version_n       integer not null,
    thread_root     text references comment (id) on delete cascade,
    anchor          jsonb not null default '{"kind": "page"}' check (json_valid(anchor)),
    quote           text not null default '',
    author_kind     text not null check (author_kind in ('human', 'agent')),
    author          text not null,
    body            text not null,
    created_at      timestamptz not null default (artifacts_now()),
    resolved_at     timestamptz,
    resolved_by     text,
    kind            text not null default 'comment'
                    check (kind in ('comment', 'remove', 'looks_good')),
    held            boolean not null default 0,
    withdrawn_at    timestamptz,
    withdrawn_kind  text,
    gg_scanned_at   timestamptz,
    gg_attempted_at timestamptz,
    review_round    integer
);
create index comment_artifact_idx on comment (artifact_id, created_at);
create index comment_thread_idx on comment (thread_root);

create table decision (
    id              text primary key,
    artifact_id     text not null references artifact (id) on delete cascade,
    version_n       integer not null,
    question        text not null,
    options         jsonb not null check (json_valid(options)),
    recommended     text,
    answer          text,
    answer_note     text not null default '',
    answered_at     timestamptz,
    answered_by     text,
    created_at      timestamptz not null default (artifacts_now()),
    closed_at       timestamptz,
    acknowledged_at timestamptz,
    acknowledged_by text,
    answer_source   text check (answer_source in
                    ('page', 'terminal', 'telegram', 'in-person', 'other')),
    recorded_by     text,
    said_at         timestamptz,
    session         text,
    kind            text not null default 'question' check (kind in ('question', 'review')),
    review_mode     text check (review_mode in ('held', 'live'))
);
create index decision_artifact_idx on decision (artifact_id, created_at);

create table decision_outcome (
    id          text primary key,
    decision_id text not null references decision (id) on delete cascade,
    body        text not null,
    agent       text not null,
    session     text,
    created_at  timestamptz not null default (artifacts_now())
);
create index decision_outcome_decision_idx on decision_outcome (decision_id, created_at);

create table review_round (
    review_id        text not null references decision (id) on delete cascade,
    n                integer not null,
    version_n        integer not null,
    base_version     integer,
    opened_at        timestamptz not null default (artifacts_now()),
    closed_at        timestamptz,
    verdict          text,
    reviewed_version integer,
    claimed_by       text,
    claim_expires_at timestamptz,
    released_reason  text,
    released_by      text,
    primary key (review_id, n)
);

-- AUTOINCREMENT: an event number is never reused after a delete, as with
-- Postgres's sequence; a reader's cursor must never see an old number again.
create table event (
    seq         integer primary key autoincrement,
    artifact_id text not null references artifact (id) on delete cascade,
    agent       text not null,
    actor       text not null,
    kind        text not null check (kind in ('decision_answered', 'comment_added',
                'thread_resolved', 'decision_shelved', 'comment_withdrawn')),
    summary     text not null default '',
    ref_id      text,
    created_at  timestamptz not null default (artifacts_now()),
    note        text not null default '',
    session     text
);
create index event_agent_seq_idx on event (agent, seq);
create index event_session_idx on event (agent, session, seq);

-- An unlabelled session is '' here, never NULL: a plain unique index then
-- folds every unlabelled reader of a seat into one row.
create table seat_read (
    agent        text not null,
    last_read_at timestamptz not null,
    session      text not null default ''
);
create unique index seat_read_seat_session_idx on seat_read (agent, session);

create table seat_grant (
    agent      text not null,
    scope      text not null,
    granted_at timestamptz not null default (artifacts_now()),
    primary key (agent, scope)
);

create table frame_pass (
    token_sha256 text primary key,
    artifact_id  text not null references artifact (id) on delete cascade,
    version      integer,
    expires_at   timestamptz not null,
    used_at      timestamptz,
    created_at   timestamptz not null default (artifacts_now()),
    changes_from integer
);
create index frame_pass_expires_idx on frame_pass (expires_at);

create table connect_request (
    id             text primary key,
    code           text not null unique,
    poll_sha256    text not null unique,
    suggested_name text,
    from_address   text not null,
    created_at     timestamptz not null default (artifacts_now()),
    expires_at     timestamptz not null,
    approved_at    timestamptz,
    agent          text,
    denied_at      timestamptz,
    delivered_at   timestamptz,
    key_id         text
);
create index connect_request_pending_idx on connect_request (from_address, expires_at);

create table page_read (
    artifact_id  text primary key references artifact (id) on delete cascade,
    read_version integer not null,
    read_at      timestamptz not null default (artifacts_now())
);

create table email_share (
    id             text primary key,
    artifact_id    text not null references artifact (id) on delete cascade,
    email          text not null,
    token_sha256   text not null unique,
    created_at     timestamptz not null default (artifacts_now()),
    revoked_at     timestamptz,
    last_opened_at timestamptz
);

create table email_share_code (
    id          text primary key,
    share_id    text not null references email_share (id) on delete cascade,
    code_sha256 text not null,
    created_at  timestamptz not null default (artifacts_now()),
    expires_at  timestamptz not null,
    attempts    integer not null default 0,
    used_at     timestamptz
);

create table email_share_device (
    cookie_sha256 text primary key,
    share_id      text not null references email_share (id) on delete cascade,
    created_at    timestamptz not null default (artifacts_now()),
    expires_at    timestamptz not null
);

create table human_pref (
    key        text primary key,
    value      text not null,
    updated_at timestamptz not null default (artifacts_now())
);

create table idempotency (
    agent       text not null,
    route       text not null,
    key         text not null,
    body_sha256 text not null,
    status      integer not null,
    response    jsonb not null check (json_valid(response)),
    expires_at  timestamptz not null,
    primary key (agent, route, key)
);
create index idempotency_expires_at on idempotency (expires_at);

create table agent_alias (
    old        text primary key,
    new        text not null,
    renamed_at timestamptz not null default (artifacts_now())
);

create table if not exists agent_key (
    id           text primary key,
    agent        text not null,
    token_sha256 text not null unique,
    created_at   timestamptz not null default now(),
    revoked_at   timestamptz
);

create table if not exists artifact (
    id              text primary key,
    title           text not null,
    agent           text not null,  -- the owning agent's SEAT name. No foreign key to agent_key,
                                    -- deliberately: a key can be revoked and reissued while its
                                    -- artifacts remain, so re-minting for the same seat keeps the
                                    -- whole history. Do not "tidy" this into a real FK, and mint
                                    -- a replacement key for the same name -- a new seat name is a
                                    -- new world with an empty listing, which is how the pages we
                                    -- are replacing got duplicated after an account rotation.
    visibility      text not null default 'private'
        check (visibility in ('private', 'link', 'public')),
    read_token      text not null,  -- the capability for reading this page on the content origin
                                    -- (design D2). A PATH segment, because the id is the hostname
                                    -- and so travels in DNS and in plaintext TLS SNI; the path is
                                    -- inside TLS. Never returned by the agent API.
    current_version integer,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create table if not exists version (
    artifact_id text not null references artifact(id) on delete cascade,
    n           integer not null,
    body        text not null,
    revision    text not null,
    status      text not null default 'live' check (status in ('live', 'pending')),
    created_by  text not null,
    created_at  timestamptz not null default now(),
    primary key (artifact_id, n)
);

create table if not exists file (
    artifact_id  text not null,
    n            integer not null,
    path         text not null,
    content_type text not null,
    bytes        bytea not null,
    primary key (artifact_id, n, path),
    -- a file belongs to a VERSION, not merely to an artifact: without this, a file row can name
    -- a version number that has never existed. Deleting the artifact still cascades, through
    -- version.
    foreign key (artifact_id, n) references version(artifact_id, n) on delete cascade
);

create table if not exists state (
    artifact_id text not null references artifact(id) on delete cascade,
    path        text not null,
    doc         jsonb not null,
    updated_at  timestamptz not null default now(),
    primary key (artifact_id, path)
);

create index if not exists artifact_agent_idx on artifact (agent);

-- artifact.current_version should point at a real version row, but the FK can only be added
-- after `version` exists (created below `artifact` above), and Postgres has no
-- ADD CONSTRAINT IF NOT EXISTS, so guard it explicitly for migrate()'s idempotency. MATCH SIMPLE
-- (the default) leaves current_version unchecked while it is NULL, which is required before the
-- first version is written.
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'artifact_current_version_fkey'
    ) then
        alter table artifact
            add constraint artifact_current_version_fkey
            foreign key (id, current_version) references version(artifact_id, n)
            on delete set null;
    end if;
end $$;

-- `version_n` records the artifact's current_version at the moment the comment was
-- written, not a live pointer to "whatever version is current now" -- a comment made
-- against version 3 must still say version 3 after the page has moved on to version 7,
-- so Jeremy's note about a paragraph that has since changed is never silently
-- re-attributed to text it was never about. No foreign key to `version`: a comment
-- survives a version being superseded (versions are never deleted, but even if that
-- changed, a comment's own history should outlive the row it was written against).
-- `thread_root` is self-referential -- a comment and its replies live in the same
-- table -- and is null exactly on a root comment; a reply names its root's id here.
-- `on delete cascade` on `thread_root` means deleting a root takes its whole thread with
-- it, the same shape `artifact_id`'s cascade already gives a whole artifact's comments.
create table if not exists comment (
    id           text primary key,
    artifact_id  text not null references artifact(id) on delete cascade,
    version_n    integer not null,
    thread_root  text references comment(id) on delete cascade,
    anchor       jsonb not null default '{"kind": "page"}'::jsonb,
    quote        text not null default '',
    author_kind  text not null check (author_kind in ('human', 'agent')),
    author       text not null,
    body         text not null,
    created_at   timestamptz not null default now(),
    resolved_at  timestamptz,
    resolved_by  text
);

create index if not exists comment_artifact_idx on comment (artifact_id, created_at);
create index if not exists comment_thread_idx on comment (thread_root);

-- A decision is an agent's lettered question, not free-form prose: `options` is a JSON
-- array of {"letter": "a", "label": "..."} in the order the agent gave them, so the
-- page can render them as a fixed sequence of buttons rather than re-sorting an object's
-- keys. `version_n` records the version the decision was posed on, exactly as
-- `comment.version_n` records the version a comment was written against -- a decision
-- asked against version 3 still says version 3 once the page has moved on to version 7.
-- `answer`/`answer_note`/`answered_at`/`answered_by` are all null (answer_note: empty
-- string) until Jeremy answers, and all move together on every answer, including a
-- second one that replaces the first -- there is no history of past answers, only the
-- current one. `closed_at` is the agent withdrawing a question it no longer needs
-- answered (it stopped mattering, or the agent moved on), distinct from an answer: a
-- decision can be answered and still open (Jeremy may change his mind), or closed with
-- no answer at all (the agent gave up waiting).
create table if not exists decision (
    id           text primary key,
    artifact_id  text not null references artifact(id) on delete cascade,
    version_n    integer not null,
    question     text not null,
    options      jsonb not null,
    recommended  text,
    answer       text,
    answer_note  text not null default '',
    answered_at  timestamptz,
    answered_by  text,
    created_at   timestamptz not null default now(),
    closed_at    timestamptz
);

create index if not exists decision_artifact_idx on decision (artifact_id, created_at);

-- The nudge journal: every interaction Jeremy has with a page -- answering a
-- decision, leaving a comment, resolving a thread -- appends exactly one row here,
-- in the SAME transaction as the write it describes (see decisions.answer, the
-- first caller), so an event can never exist without its cause and a cause can
-- never be recorded without its event. `agent` is the seat the event is FOR (the
-- artifact's owner), not who performed it -- Jeremy is never a seat, so there is
-- nothing else `agent` could usefully name. `seq` is a bigserial: monotonic per
-- database, never reset, which is what lets `?since=<seq>` be a cursor an agent
-- keeps rather than a timestamp two events could tie on. This table is read from
-- ANYWHERE with a key (`GET /api/v1/events`); the push to baba's ops inbox
-- (`nudge.py`) is only an optimisation layered on top -- if it never fires,
-- `?since=` still delivers everything.
create table if not exists event (
    seq         bigserial primary key,
    artifact_id text not null references artifact(id) on delete cascade,
    agent       text not null,
    kind        text not null,
    summary     text not null default '',
    ref_id      text,
    created_at  timestamptz not null default now()
);

create index if not exists event_agent_seq_idx on event (agent, seq);

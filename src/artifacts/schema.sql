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

-- ACKNOWLEDGEMENT is "an agent has SEEN this answer", which is not the same fact as
-- "the work is done" (`closed_at`) and not the same as "Jeremy answered"
-- (`answered_at`). It exists because the alert that catches a lost answer has to
-- distinguish a slow owner from a dead one, and "the page was republished" cannot do
-- that: most answers here are acted on by doing infrastructure work over hours -- a
-- commit, a playbook, an SSH session -- so an alert waiting for completion fires on
-- correctly handled decisions, gets muted, and then misses the real one.
-- Set once and never moved: the question the alert asks is "did anyone hear this",
-- and the first hearing is the answer to it. Closing implies acknowledgement, because
-- an agent that finished the work necessarily heard the answer -- and an agent that
-- just does its job should pay nothing extra.
alter table decision add column if not exists acknowledged_at timestamptz;
alter table decision add column if not exists acknowledged_by text;

-- The nudge journal: every interaction Jeremy has with a page -- answering a
-- decision, leaving a comment, resolving a thread -- appends exactly one row here,
-- in the SAME transaction as the write it describes (see decisions.answer, the
-- first caller), so an event can never exist without its cause and a cause can
-- never be recorded without its event. `agent` is the seat the event is FOR (the
-- artifact's owner); `actor` is WHO did it ("jeremy" for a gallery form, a seat
-- name for an agent-initiated write) -- kept as its own column, never inferred
-- from `kind` or from `agent`, because an event means "something happened on
-- your page that you did not do": collapsing actor into agent would, the moment
-- an agent-initiated write is ever journalled, tell that same agent that Jeremy
-- did something it did itself. `seq` is a bigserial: monotonic per database,
-- never reset, which is what lets `?since=<seq>` be a cursor an agent keeps
-- rather than a timestamp two events could tie on. This table is read from
-- ANYWHERE with a key (`GET /api/v1/events`); the push to baba's ops inbox
-- (`nudge.py`) is only an optimisation layered on top -- if it never fires,
-- `?since=` still delivers everything.
--
-- All three `kind`s the design names are enumerated in the CHECK below even
-- though only `decision_answered` has a live producer this slice
-- (`decisions.answer`) -- `comment_added` and `thread_resolved` wait for a
-- Jeremy-initiated comment/resolve path in a later slice (see
-- docs/comments-design.md's own slice list). Enumerating all three now means
-- that later slice is a line of code, never a migration to widen this CHECK.
create table if not exists event (
    seq         bigserial primary key,
    artifact_id text not null references artifact(id) on delete cascade,
    agent       text not null,
    actor       text not null,
    kind        text not null
        check (kind in ('decision_answered', 'comment_added', 'thread_resolved')),
    summary     text not null default '',
    ref_id      text,
    created_at  timestamptz not null default now()
);

create index if not exists event_agent_seq_idx on event (agent, seq);

-- When each seat last asked for its own events.
--
-- One row per seat, overwritten on every read: this is not a log, it is the
-- answer to one question -- "is anybody still listening on this seat?" -- and
-- the alert that catches a lost answer cannot work without it. An answer that
-- was given and never collected has two completely different causes, needing
-- opposite responses: the agent is gone (nobody has read this feed since the
-- answer, so someone else must collect it), or the agent is alive and stuck
-- (it has read the feed since, and still has not taken the answer). Without a
-- last-read time those two look identical, and an alert that cannot tell them
-- apart teaches whoever reads it to ignore both.
--
-- Written at the START of a read, not the end: a waiter parked in a 280-second
-- hold has been listening for all 280 of those seconds, and recording the time
-- it gave up would make the most attentive listener look like the most absent.
create table if not exists seat_read (
    agent        text primary key,
    last_read_at timestamptz not null
);

-- A named privilege held by a SEAT, not by one of its keys.
--
-- One grant exists today: `decisions:read-all`, which lets a seat read every
-- open question across every page instead of only its own. It exists because of
-- what 20 Sep 2026 showed -- a question posed on one agent's page sat
-- unanswered for seventeen hours because nothing could see it except the seat
-- that asked. An operator that can only know what each agent remembers to tell
-- it cannot answer "what is waiting on Jeremy", and that question is the whole
-- point of the service.
--
-- Held by the seat rather than the key so that rotating a key keeps the
-- privilege, and revoking the seat's grant removes it from every key at once.
-- Default is no grant at all: a seat has this only because someone typed
-- `artifacts-admin grant-scope`, and `revoke-scope` takes it back.
create table if not exists seat_grant (
    agent      text not null,
    scope      text not null,
    granted_at timestamptz not null default now(),
    primary key (agent, scope)
);

-- AN ANSWER JEREMY GAVE SOMEWHERE ELSE (homelab-ij8.78). He answers in a terminal
-- or on Telegram as often as on a page, and before this there was nothing honest a
-- seat could do with those words: pressing the button on his behalf mints an answer
-- attributed to him, on the page, INDISTINGUISHABLE from one he tapped -- the kind
-- of record that gets believed later by an agent that was not there. Two seats hit
-- this on 20 Sep 2026 and both correctly refused, leaving four real answers stranded
-- with the decisions still open.
--
-- `answer_source` is a VALUE, never a boolean and never a null-means-tapped: an
-- unaware reader must not be able to mistake a relayed answer for a tapped one, and
-- the way to guarantee that is to make every answered row say which it was. Existing
-- answers are backfilled to 'page' below, so `answer is not null and answer_source
-- is null` is never a state anyone has to interpret.
--
-- `recorded_by` is the SEAT that wrote it down, and it is required for a relay. An
-- anonymous relay is the same forgery wearing a different hat: the accountable party
-- is part of the record, not metadata about it. It stays null for a page tap, where
-- `answered_by` already says jeremy and no seat was involved.
--
-- `said_at` is when HE said it, which is not when a seat got round to recording it.
-- `answered_at` keeps meaning "when this row gained its answer" so the uncollected
-- alert and the event journal need no special case.
alter table decision add column if not exists answer_source text
    check (answer_source in ('page', 'terminal', 'telegram', 'in-person', 'other'));
alter table decision add column if not exists recorded_by text;
alter table decision add column if not exists said_at timestamptz;

update decision set answer_source = 'page'
    where answer is not null and answer_source is null;

-- A PAGE'S THUMBNAIL, ATTACHED TO THE VERSION IT DEPICTS (homelab-ij8.22).
-- On `version`, not on `artifact`, and that placement is the whole correctness
-- argument: a picture stored against the page would survive the page changing
-- under it, and a stale thumbnail is precisely the drift this service exists to
-- prevent -- worse than no picture, because Jeremy would read the wall as
-- current. Keyed to the version, a thumbnail is either of that version or
-- absent, and it goes when the version goes.
--
-- Null is the ordinary state: never rendered yet, rendered and failed, or a
-- deployment with no browser at all. The wall falls back to its text tile,
-- which is a complete tile in its own right and not a placeholder.
alter table version add column if not exists thumbnail bytea;

-- ARCHIVING (Jeremy, 20 Sep 2026: "I would also like the ability to archive so
-- that they don't appear in my gallery"). A page whose work is done should stop
-- competing for his attention without ceasing to exist: the address keeps
-- working, the versions stay, the decisions and the answers he gave on it stay
-- readable, and it simply leaves the wall.
--
-- Deliberately NOT deletion, and deliberately reversible. Pages here carry the
-- record of what he was asked and what he decided; the cheap way to tidy a
-- gallery must not be the way that record gets destroyed. Unarchiving is a
-- single column write.
alter table artifact add column if not exists archived_at timestamptz;

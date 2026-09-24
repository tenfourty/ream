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

-- PINNING (Jeremy, 20 Sep 2026, from the claude.ai gallery: "you can also pin
-- items in the gallery"). A pinned page leads the wall regardless of when it
-- last changed, because "what I care about" and "what moved most recently" are
-- different orderings and the gallery has only ever had the second one.
alter table artifact add column if not exists pinned_at timestamptz;

-- THE NOTE TRAVELS WITH THE EVENT (homelab-ij8.* , 20 Sep 2026).
--
-- `summary` for an answered decision is "letter: option label" and nothing
-- else, so every consumer of this feed -- the waiter, the operator's mailbox,
-- any future client -- learned that Jeremy had answered while being told
-- nothing about what he actually wrote. This service's stated principle, in
-- bold in its own skill, is that HIS NOTE IS THE DECISION: every note he has
-- written has qualified the letter it came with, and one "(a)" of his read
-- "start with (b) but then (a) if we can".
--
-- It cost a real afternoon. On 20 Sep the operator worked from a letter whose
-- note said "check all folders between the two backups, then remove anything
-- that is already elsewhere... in particular the photo libraries from iphoto",
-- and never saw it, because the event could not carry it. Another seat was
-- told "(a)" on a question whose note read "explain this to me first, there is
-- not enough context for me to decide on this!".
--
-- Its own column rather than appended to `summary`: a consumer that renders a
-- summary in one line must be able to render the note differently -- longer,
-- quoted, and impossible to skim past -- and concatenating them would make
-- that impossible for every consumer at once.
alter table event add column if not exists note text not null default '';

-- DELETION, WITH THIRTY DAYS TO CHANGE YOUR MIND (Jeremy, 20 Sep 2026, answering
-- on page mwfspjce6vgtbsifil5kngpglx: (a) "Delete it, but keep it recoverable
-- for thirty days -- plus a separate destroy-it-now", and in chat immediately
-- after: "ideally the user and the agent can both recover deleted pages").
--
-- A nullable column, exactly like `archived_at`, and for the same reason: what
-- must survive the window is the body, every version, and every decision with
-- his answers and his notes. Deleting is the cheap way to tidy and it must not
-- be the way the record of what he decided is destroyed -- which is
-- homelab-ij8.44's lesson, and that one bit twice in a single day.
--
-- Archiving and deleting are SEPARATE columns rather than one state field. A
-- page can be archived and then deleted, and restoring it should put it back
-- where it was rather than onto the wall; one field would have to encode that
-- and would lose it.
--
-- DESTROY is not a column. It is an actual DELETE of the artifact row, and the
-- foreign keys take the versions, decisions and comments with it. There is no
-- flag for "really gone", because a flag for it is exactly the hidden row this
-- window is supposed to stop being permanent.
alter table artifact add column if not exists deleted_at timestamptz;

-- Every listing filters on this, so it is worth an index even at this size:
-- the common query is "not deleted", which without one is a sequential scan
-- that grows with everything ever deleted rather than with what is live.
create index if not exists artifact_live_idx on artifact (deleted_at)
  where deleted_at is null;

-- A FOURTH EVENT KIND: the page holding an open question left the wall
-- (homelab-ij8.84.10, 21 Sep 2026).
--
-- Found by walking the gallery as a user. Archive or delete a page with an
-- unanswered question and the question stayed open, Jeremy stopped being shown
-- it, and THE AGENT THAT ASKED WAS NEVER TOLD -- its waiter held until its own
-- timeout, every time, unable to tell "not answered yet" from "he will not see
-- this". A seat can lose a shift to that.
--
-- `decision_shelved`, not `decision_abandoned`: both archiving and deleting are
-- reversible here, so the honest claim is "this left his wall", not "this will
-- never be answered". The summary says which of the two happened. An agent
-- hearing it should stop WAITING and tell its human; it should not conclude the
-- question is dead.
--
-- The CHECK has to be dropped and re-added rather than widened in place, which
-- is why this is its own migration and not a line. The three original kinds
-- were enumerated up front precisely so that adding one would be deliberate.
alter table event drop constraint if exists event_kind_check;
alter table event add constraint event_kind_check
    check (kind in ('decision_answered', 'comment_added', 'thread_resolved',
                    'decision_shelved'));

-- WHO, when three seats share one key (homelab-ij8.32, 21 Sep 2026).
--
-- Every page on this service is owned by 'baba-operator', because that is the
-- one key on this machine and every session on it reads the same file. So the
-- service is not confused about whose work it is -- it is correct, and we are
-- one agent with three heads. Measured 21 Sep: 4 seat names exist, 1 live key
-- for baba-operator, 10 of the 13 live pages under it.
--
-- What that costs, in the order it bites:
--   * A waiter is woken by another seat's decision and cannot tell. Reported
--     twice in ten minutes by homelab-31 on 21 Sep; both wakes were this seat's
--     own test probe. "A watcher which cries wolf trains its reader to stop
--     looking, and the one wake that matters is indistinguishable from these."
--   * seat_read is keyed by agent, so "is anybody still listening on this
--     seat?" answers for whichever of the three read most recently. The alert
--     that catches a lost answer is built on a fact that is not true yet.
--   * A bead claim cannot name who holds it (homelab-6t4) -- the same bug one
--     layer up.
--
-- JEREMY RULED, 20 Sep 2026, verbatim: "(a) one key per agent session -- maybe
-- something like a key per machine/host plus some sort of uuid for the agent
-- sessions? any agent with a key could theoretically edit another agents
-- artifact or read it but that is okay if they are authenticated."
--
-- So the three things come apart and only the middle one is new:
--   AUTHENTICATION stays per key, at one key per machine. Not per session.
--   ATTRIBUTION is this column. A label the caller declares, nothing more.
--   AUTHORISATION stays OPEN across seats sharing a key. He said so explicitly.
--
-- THIS COLUMN IS NOT A PERMISSION AND MUST NEVER BECOME ONE. It is declared by
-- the caller, so it is unforgeable by nobody at all -- any key holder can send
-- any value. Scoping a read or a write to it would be a check that a one-word
-- change to an environment variable defeats, which is worse than no check
-- because it reads like one. `_scoped` stays keyed on `agent`. Filtering
-- /events by session is a CONVENIENCE for the caller filtering its own feed,
-- not a boundary: the rows were already all visible to that key.
--
-- Nullable, with no default, and every existing row keeps NULL. A page
-- published before this cannot say which session made it, and inventing an
-- answer for those rows would be exactly the fabrication this service exists
-- to prevent.
alter table artifact add column if not exists session text;
alter table version  add column if not exists session text;
alter table decision add column if not exists session text;
alter table event    add column if not exists session text;

-- The filter homelab-31 asked for, and the only index needed: a feed read is
-- already (agent, seq) and this narrows within it.
create index if not exists event_session_idx on event (agent, session, seq);

-- WHICH SESSION LAST READ THE FEED, not merely which machine (homelab-ij8.64).
--
-- seat_read answers "is anybody still listening on this seat?", and the alert
-- for a stranded answer is built entirely on it: a decision answered but never
-- acknowledged is `no_reader` if nothing has read since, and `stuck` if
-- something has. Those need opposite responses -- collect it on the agent's
-- behalf, or go and look at a live agent that is not acting.
--
-- Keyed on `agent` alone, it answered for whichever of the three sessions
-- sharing baba's one key read most recently. So the alert could report a live
-- listener while the session that actually asked had been gone for hours --
-- exactly the wrong answer in the one case the feature exists for. Until
-- sessions landed this morning there was no way to tell them apart.
--
-- NULLS NOT DISTINCT (Postgres 15+; 16.2 in tests, 18.1 in the cluster) so that
-- every caller which declares no session shares ONE row rather than inserting a
-- new one on every read. Without it, `on conflict` never fires for them --
-- two NULLs are normally distinct to a unique index -- and this table grows a
-- row per request for ever.
--
-- The primary key goes because a primary key cannot hold NULL, and NULL is the
-- honest value for "this caller did not say". Dropping it costs nothing: the
-- unique index below is what `on conflict` needs, and nothing references this
-- table.
alter table seat_read add column if not exists session text;
alter table seat_read drop constraint if exists seat_read_pkey;
create unique index if not exists seat_read_seat_session_idx
    on seat_read (agent, session) nulls not distinct;

-- WHAT A DECISION CAUSED, and what changed under it (homelab-ij8.44).
--
-- The model was question -> options -> answer -> answer_note, and stopped. The
-- interesting part arrives later: Jeremy chose (a), and what that PRODUCED was
-- a PR, a closed bead and a correction to the agent's own framing. There was
-- nowhere to put that, so an agent hand-edited its page's HTML to say
-- "Done - PR #14, and the job was smaller than I claimed" -- the durable record
-- of what a decision caused living only in prose somebody happened to rewrite.
--
-- A TABLE, NOT A COLUMN, and not gated on the decision being answered. The
-- operator's case, 21 Sep: they corrected two promises on a page WHILE THE
-- QUESTION WAS STILL OPEN. That one has teeth -- a question whose premise moved
-- after Jeremy read it and before he answered is one he can answer wrongly
-- through no fault of his own, and nothing on the page said so.
--
-- APPEND ONLY. No update, no delete, and deliberately no "current outcome"
-- field anywhere. Several notes accumulate, none replaces another, because the
-- failure this fixes is exactly a summary that somebody keeps rewriting until
-- what actually happened is gone. Deleting the page still cascades; nothing
-- else removes one.
--
-- `session` alongside `agent` because sessions landed this morning: three seats
-- share one key, so "who recorded this" is otherwise answerable only as "that
-- machine". Nullable like every other session column -- NULL means the writer
-- did not say.
create table if not exists decision_outcome (
    id          text primary key,
    decision_id text not null references decision(id) on delete cascade,
    body        text not null,
    agent       text not null,
    session     text,
    created_at  timestamptz not null default now()
);

-- Read path is always "every outcome for this decision, oldest first", which is
-- the order they happened and the order they are read.
create index if not exists decision_outcome_decision_idx
    on decision_outcome (decision_id, created_at);

-- A PASS TO READ ONE PAGE, ONCE, SOON (homelab-ij8.101, 22 Sep 2026).
--
-- Jeremy's ruling, confirmed: a private page is readable only by him, from
-- anywhere, after his normal login. Until now the thing that let a browser read
-- a page body was `artifact.read_token`, rendered into the gallery's iframe and
-- valid for ever -- so once page origins face the internet, one leaked frame URL
-- would be a permanent public read of that page.
--
-- Now the gallery, which is behind his login, mints one of these each time it
-- shows a page, and the content origin accepts it ONCE, within minutes. Modelled
-- on how claude.ai frames artifacts (inspected on his request the same day),
-- minus its measured weakness: its pass reads the page, reusable, for an hour.
--
-- Hashed at rest like agent keys: a database dump must not be a stack of
-- working passes, however short-lived. `version` NULL means "the live page".
-- Rows are swept by `frame_pass.mint` itself, so the table stays the size of
-- the last few minutes' page views without a job of its own.
create table if not exists frame_pass (
    token_sha256 text primary key,
    artifact_id  text not null references artifact(id) on delete cascade,
    version      integer,
    expires_at   timestamptz not null,
    used_at      timestamptz,
    created_at   timestamptz not null default now()
);

create index if not exists frame_pass_expires_idx on frame_pass (expires_at);

-- A MACHINE ASKING TO JOIN (homelab-ij8.102, 23 Sep 2026).
--
-- Jeremy's confirmed ruling: an arriving agent asks for access and shows him a
-- short code; he types that code into the gallery, names the machine, and
-- approves; the key goes straight to the machine and is never shown to him,
-- never in a prompt, never in a chat. The TV sign-in pattern.
--
-- NO KEY IS STORED HERE, EVER. Approval records only the name he chose; the
-- key is minted at the moment the machine next polls, returned in that one
-- response, and only its hash exists anywhere after that (agent_key). A dump
-- of this table is a list of names and codes, not of credentials.
--
-- `poll_sha256`: the machine holds a random poll token; only its hash is kept,
-- so the code on his screen (short, typed by a human) is never itself enough
-- to collect the key -- the collector must also hold the poll token.
create table if not exists connect_request (
    id             text primary key,
    code           text not null unique,
    poll_sha256    text not null unique,
    suggested_name text,
    from_address   text not null,
    created_at     timestamptz not null default now(),
    expires_at     timestamptz not null,
    approved_at    timestamptz,
    agent          text,
    denied_at      timestamptz,
    delivered_at   timestamptz,
    key_id         text
);

create index if not exists connect_request_pending_idx
    on connect_request (from_address, expires_at);

-- WHAT JEREMY HAS READ (homelab-ij8.103, 23 Sep 2026).
--
-- His confirmed ruling: a page is UNREAD when it is new or has been
-- republished since he last opened it; the wall puts unread pages after pinned
-- ones and pages with questions for him; and the agent that published a page
-- can see whether he has opened its latest version. One row per page, because
-- the gallery has exactly one reader. Written when he opens the page in the
-- gallery, never by an agent.
create table if not exists page_read (
    artifact_id  text primary key references artifact(id) on delete cascade,
    read_version integer not null,
    read_at      timestamptz not null default now()
);

-- WHO ELSE MAY READ A PAGE (homelab-ij8.105, 23 Sep 2026).
--
-- Jeremy's confirmed ruling: by default every agent connected to his space can
-- READ every page in it -- the page, its versions, its questions and his
-- answers -- whoever published it. A page can be closed to just him and its
-- publisher, by him in the gallery or by the publisher. Only the publisher ever
-- changes a page.
--
-- Existing pages start CLOSED (the update below runs once, on the rows that
-- predate the column), because he asked to see the list and close any before
-- they open. New pages are open from their first insert (the default).
alter table artifact add column if not exists agents_can_read boolean;
update artifact set agents_can_read = false where agents_can_read is null;
alter table artifact alter column agents_can_read set default true;
alter table artifact alter column agents_can_read set not null;

-- THE MACHINES PAGE (homelab-ij8.103, 23 Sep 2026). Jeremy's confirmed ask: see
-- every connected machine, rename it, revoke it. `label` is the name he gives a
-- machine on that page; `agent` stays the identity its pages and questions are
-- filed under, so renaming never rewrites history. `last_used_at` is touched by
-- a successful key check at most every few minutes (keys.touch).
alter table agent_key add column if not exists label text;
alter table agent_key add column if not exists last_used_at timestamptz;

-- A PAGE SHARED BY LINK (homelab-ij8.101, 23 Sep 2026). Jeremy's rulings: a
-- deliberate act per page, his alone; the link shows the LATEST version, just
-- the page, full screen on its own address; he can withdraw it at any time and
-- it dies at once (the row is deleted). One live link per page. The token is
-- kept in plaintext because he needs to see and copy the link again -- it is a
-- capability he hands out on purpose, not a credential anyone logs in with.
create table if not exists artifact_share (
    artifact_id text primary key references artifact(id) on delete cascade,
    token       text not null unique,
    created_at  timestamptz not null default now()
);

-- KEYS THAT REPLACE THEMSELVES (homelab-ij8.34, Jeremy's (3a), 24 Sep 2026).
-- `rotating`: a key a machine received by connecting (artifacts-connect). Its
-- tools swap it weekly; one never swapped is refused after ROTATING_MAX_AGE.
-- House seats' keys (vaulted, encrypted at rest) are not rotating.
-- `replaces`: the key this one succeeds. The old key dies when the new one is
-- FIRST USED (`first_used_at`), so a lost rotate response never locks a machine
-- out -- and a thief who rotates first kills the real machine's key, loudly.
-- `revoked_reason`: 'replaced' tells a machine locked out that way what it means.
alter table agent_key add column if not exists rotating boolean not null default false;
alter table agent_key add column if not exists replaces text;
alter table agent_key add column if not exists first_used_at timestamptz;
alter table agent_key add column if not exists revoked_reason text;

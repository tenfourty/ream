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

-- What kind of mark a comment is (page review, homelab-ij8.108, 28 Sep 2026):
-- a remark, "remove this", or "looks good". A reply is always 'comment'.
alter table comment add column if not exists kind text not null default 'comment'
    check (kind in ('comment', 'remove', 'looks_good'));

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
    kind        text not null,
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
alter table decision add column if not exists answer_source text;
-- No CHECK on it: the stored values are validated in code (decisions.stored_sources()).
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
-- (Superseded 2 Nov 2026: `decision_shelved` is no longer written; archiving or binning
-- now emits `decision_set_aside`, and restoring emits `decision_reopened`.)
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
-- (Since slice 5 of the core-and-plugins design the CHECK is gone altogether: the kind is
-- validated in code, `events.append` against `events.KINDS`. The block that drops it from a
-- database that still has it is further down, by the partner bridge's answer source.)

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

-- A review request is a decision with four fixed verdicts (page review step 2,
-- homelab-ij8.108, 29 Sep 2026). 'question' is every lettered question before it.
alter table decision add column if not exists kind text not null default 'question'
    check (kind in ('question', 'review'));

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

-- A pass for the CHANGES VIEW (page review step 4): the live page marked up
-- against this earlier version. NULL for the plain page. The view is drawn
-- when served and never stored as a version.
alter table frame_pass add column if not exists changes_from integer;

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

-- SHARING A PAGE WITH ONE PERSON BY EMAIL (homelab-ij8.16, Jeremy's (1a),
-- 24 Sep 2026). Each person gets their own link; the first time they open it
-- on a device they confirm with a one-time code sent to the same inbox, so a
-- forwarded email lets nobody else in. He sees who has access and withdraws
-- each person separately. Only HASHES of the link token, the codes and the
-- device cookies are stored.
create table if not exists email_share (
    id             text primary key,
    artifact_id    text not null references artifact(id) on delete cascade,
    email          text not null,
    token_sha256   text not null unique,
    created_at     timestamptz not null default now(),
    revoked_at     timestamptz,
    last_opened_at timestamptz
);

create table if not exists email_share_code (
    id          text primary key,
    share_id    text not null references email_share(id) on delete cascade,
    code_sha256 text not null,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null,
    attempts    integer not null default 0,
    used_at     timestamptz
);

create table if not exists email_share_device (
    cookie_sha256 text primary key,
    share_id      text not null references email_share(id) on delete cascade,
    created_at    timestamptz not null default now(),
    expires_at    timestamptz not null
);

-- WHAT THE PAGE DID WHEN IT WAS RENDERED (homelab-ij8.72). The thumbnail render
-- also collects the page's uncaught script errors from the browser's console;
-- they are stored with the version they came from and reported to its author.
-- NULL render_errors with a NULL rendered_at means "not rendered (yet)".
alter table version add column if not exists render_errors jsonb;
alter table version add column if not exists rendered_at timestamptz;

-- HOLD OR LIVE (page review step 3, homelab-ij8.108). A mark Jeremy makes
-- while its page is under review in 'held' mode is invisible to the agent
-- until his verdict or until he switches to 'live'.
alter table comment add column if not exists held boolean not null default false;
alter table decision add column if not exists review_mode text
    check (review_mode in ('held', 'live'));
create table if not exists human_pref (
    key        text primary key,
    value      text not null,
    updated_at timestamptz not null default now()
);

-- A comment GitGuardian flagged after it was saved (homelab-ij8.112): its
-- text is blanked and it is hidden from every read; the kind stays.
alter table comment add column if not exists withdrawn_at timestamptz;
alter table comment add column if not exists withdrawn_kind text;

-- When GitGuardian last gave an answer on an agent's comment (homelab-ij8.112).
-- Null means it has not been checked yet -- a restart killed the background
-- task, or the relay was unreachable -- and a sweep at startup and every ten
-- minutes retries it.
--
-- BACKFILL: every comment saved before this column existed was already checked
-- by GitGuardian BEFORE it was saved (the old behaviour), so the column is
-- created with default now() to mark those rows scanned exactly once, and the
-- default is dropped straight after so new rows start null. Re-running is a
-- no-op: the add does nothing once the column exists, and dropping a default
-- that is gone does nothing.
alter table comment add column if not exists gg_scanned_at timestamptz default now();
alter table comment alter column gg_scanned_at drop default;

-- The last time the sweep tried this comment, answered or not, so one comment
-- the relay keeps refusing cannot hold the head of the queue for ever.
alter table comment add column if not exists gg_attempted_at timestamptz;

-- REVIEW ROUNDS (page review step 4, homelab-ij8.108). Round 1 opens when the
-- agent asks for a review; a republish after "changes requested" opens the
-- next one by itself. `base_version` is the version the previous round's
-- "changes requested" was given on (null for round 1), and it never moves;
-- `version_n` follows the page while the round is open. A verdict closes the
-- round and is kept here after the decision row is cleared for the next one.
create table if not exists review_round (
    review_id    text not null references decision(id) on delete cascade,
    n            int not null,
    version_n    int not null,
    base_version int,
    opened_at    timestamptz not null default now(),
    closed_at    timestamptz,
    verdict      text,
    primary key (review_id, n)
);
-- Reviews asked before rounds existed get their round 1, once. Idempotent:
-- every review already has a round 1 after the first run.
insert into review_round (review_id, n, version_n, base_version, opened_at,
                          closed_at, verdict)
    select id, 1, version_n, null, created_at, answered_at,
           case answer when 'a' then 'approved'
                       when 'b' then 'approved_with_notes'
                       when 'c' then 'changes_requested'
                       when 'd' then 'dismissed' end
    from decision where kind = 'review'
    on conflict (review_id, n) do nothing;
-- The round a mark was made in; null outside any open round.
alter table comment add column if not exists review_round int;
-- The version the page view showed when he gave the round's verdict (the
-- form's hidden field). The next round diffs from it; null for a relay, which
-- falls back to the round's `version_n`.
alter table review_round add column if not exists reviewed_version int;

-- CLAIMS (page review step 5). One session acts on a round: `wait --review`
-- claims the current round with a lease it renews while it runs, and a second
-- session is told who holds it. A session that cannot act hands the round back
-- with a reason. Per round, so a new round starts unclaimed.
alter table review_round add column if not exists claimed_by text;
alter table review_round add column if not exists claim_expires_at timestamptz;
alter table review_round add column if not exists released_reason text;
-- The session that handed the round back, for his card; a claim clears it.
alter table review_round add column if not exists released_by text;

-- IDEMPOTENCY (page review step 5). A retried reply or acknowledgement sends
-- the same `Idempotency-Key`; the first response is kept for 24 hours and a
-- repeat gets it back without writing anything. Scoped per agent and route.
create table if not exists idempotency (
    agent       text        not null,
    route       text        not null,
    key         text        not null,
    body_sha256 text        not null,
    status      int         not null,
    response    jsonb       not null,
    expires_at  timestamptz not null,
    primary key (agent, route, key)
);

-- Expired rows are pruned a bounded batch at a time; the index keeps finding
-- them from being a scan of the whole table inside the held transaction.
create index if not exists idempotency_expires_at on idempotency (expires_at);

-- WHAT AN AGENT USED TO BE CALLED (6 Oct 2026). Jeremy renamed every
-- permanent agent from "seat" to "<area> maintainer"; `artifacts-admin
-- rename-agent` rewrites every row that names the old agent, and leaves one row
-- here so a name somebody TYPES (give a page to, filter the wall by, grant a
-- scope to) still means the agent it became. One hop only: a rename re-points
-- every alias that pointed at its old name, so `new` is never itself an alias.
-- Authentication needs none of this -- the key is the identity, and its row is
-- rewritten with everything else.
create table if not exists agent_alias (
    old        text primary key,
    new        text not null,
    renamed_at timestamptz not null default now()
);

-- A SECTION UPDATE'S OWN RECORD (6 Oct 2026). A version written by
-- `PATCH /artifacts/{id}/sections/{el}` says what moved: `note` is
-- "#<id>: <op>" plus the caller's note, `section` is the part's name as his
-- wall shows it (its heading, a data-section-title, or the id), and `notify`
-- is the publisher asking for his eye -- the wall marks that tile. NULL /
-- false on every whole-page version, which is the honest value: those say
-- nothing about which part changed.
alter table version add column if not exists note    text;
alter table version add column if not exists section text;
alter table version add column if not exists notify  boolean not null default false;

-- seat_read: an unlabelled session is '' rather than NULL (13 Oct 2026, the
-- SQLite switch, slice 2). SQLite has no NULLS NOT DISTINCT, so a plain unique
-- index can only fold every unlabelled caller into one row if they share a
-- real value. Idempotent: a NULL row written by an older pod during a rollout
-- merges into the '' row (the later read wins) before the rest are renamed.
update seat_read t set last_read_at = n.last_read_at from seat_read n
    where t.session = '' and n.session is null and n.agent = t.agent
    and n.last_read_at > t.last_read_at;
delete from seat_read where session is null and exists (
    select 1 from seat_read e where e.agent = seat_read.agent and e.session = '');
update seat_read set session = '' where session is null;
alter table seat_read alter column session set default '';
alter table seat_read alter column session set not null;

-- Which session an agent's comment was written under (21 Oct 2026, an agent
-- comments on another office's page): the label the caller declared in
-- X-Artifacts-Session. NULL on every earlier comment, on Jeremy's, and on a
-- caller that declared none -- the honest value, never a guess.
alter table comment add column if not exists session text;

-- WHAT A QUESTION CARRIES (runcove-5t3oj.39.1, 21 Oct 2026): the context he
-- needs to answer it, the topic it belongs to, what it depends on and
-- replaces, how urgent it is, and which other offices need the answer.
-- All asked of 'question' decisions; a review keeps the defaults.
alter table decision add column if not exists context text not null default '';
alter table decision add column if not exists topic_id text;
alter table decision add column if not exists topic_title text;
alter table decision add column if not exists depends_on jsonb not null default '[]';
-- Plain text, not foreign keys: the ids are checked when the question is asked
-- (and answer as one "not yours" whether unknown or someone else's), and a
-- deleted page must not be blocked by, or take with it, a question that named it.
alter table decision add column if not exists replaces text;
alter table decision add column if not exists replaced_by text;
alter table decision add column if not exists urgency text not null default 'when_you_can';
alter table decision add column if not exists blocked_on text;
alter table decision add column if not exists needed_by timestamptz;
alter table decision add column if not exists for_offices jsonb not null default '[]';

-- THE PARTNER OUTBOX (runcove-5t3oj.39.4, 21 Oct 2026): one row per event a
-- question's life gives the partner service (partner.py). Written in the SAME
-- transaction as the change that causes it, delivered later by a background
-- loop. No foreign key to `decision`: a purged page must not take with it an
-- event not yet delivered. `partner_version` is the per-question counter; the
-- update that bumps it takes the row lock, so two events for one question never
-- share a version, and (decision_id, version) is unique as a second guard.
-- `body` is the event's own body only: key_id and sent_at are added, and the
-- whole signed, on each delivery attempt.
alter table decision add column if not exists partner_version integer not null default 0;
create table if not exists partner_outbox (
    event_id        text primary key,
    decision_id     text not null,
    version         integer not null,
    kind            text not null,
    body            jsonb not null,
    created_at      timestamptz not null,
    attempts        integer not null default 0,
    next_attempt_at timestamptz not null,
    claimed_until   timestamptz,
    delivered_at    timestamptz,
    last_error      text,
    last_error_at   timestamptz,
    unique (decision_id, version)
);
-- A refusal retrying cannot change parks the row, and holds its question's
-- later versions; `artifacts-admin partner-outbox --retry` releases it.
alter table partner_outbox add column if not exists parked_at timestamptz;
create index if not exists partner_outbox_due_idx
    on partner_outbox (next_attempt_at) where delivered_at is null;

-- THE PARTNER BRIDGE, INBOUND (runcove-5t3oj.39.4, second half; partner_in.py).
--
-- 'slack' is an answer source: Jeremy pressed a button in Slack and the partner
-- (quarterdeck) vouched for who pressed. The CHECK is dropped and re-added, as
-- `event_kind_check` is, because it cannot be widened in place. Agents cannot
-- record 'slack' (`decisions.ANSWER_SOURCES` stays what a relay may claim).
-- Since slice 5 of the core-and-plugins design neither this CHECK nor `event.kind`'s exists:
-- both are validated in code (`decisions.stored_sources()`, `events.KINDS`), so a plug-in can
-- add a value without a migration. Drop whatever check constraint on those two columns a
-- database still has, found in pg_constraint by what it tests rather than by name (the
-- inline-created and the re-added constraint are both `<table>_<column>_check`, but a
-- restored dump may differ). Idempotent: a database without them finds nothing.
do $$
declare c record;
begin
    for c in
        select con.conrelid::regclass as tbl, con.conname
        from pg_constraint con
        where con.contype = 'c'
          and con.connamespace = current_schema()::regnamespace
          and ((con.conrelid = 'decision'::regclass
                and pg_get_constraintdef(con.oid) ~ '^CHECK \(\(?answer_source\M')
            or (con.conrelid = 'event'::regclass
                and pg_get_constraintdef(con.oid) ~ '^CHECK \(\(?kind\M'))
    loop
        execute format('alter table %s drop constraint %I', c.tbl, c.conname);
    end loop;
end $$;

-- A question whose home is the partner: the copy artifacts shows him. `source`
-- is "qd:<id>"; `version` is the HOME's last applied version. `out_version` is
-- OUR counter for the events we send about the copy (his answer given here): a
-- separate space, so it can never collide with the home's.
create table if not exists partner_question (
    source          text primary key,
    version         integer not null,
    asking_office   text not null,
    title           text not null,
    options         jsonb not null,
    recommended     text,
    context         text not null default '',
    link            text,
    topic_id        text,
    topic_title     text,
    depends_on      jsonb not null default '[]',
    replaces        text,
    replaced_by     text,
    urgency         text not null default 'when_you_can',
    blocked_on      text,
    needed_by       timestamptz,
    for_offices     jsonb not null default '[]',
    state           text not null default 'open'
                    check (state in ('open', 'answered', 'withdrawn', 'replaced')),
    answer          text,
    answer_note     text not null default '',
    answered_by     text,
    answered_at     timestamptz,
    pending_answer  text,
    pending_note    text not null default '',
    pending_at      timestamptz,
    out_version     integer not null default 0,
    created_at      timestamptz not null,
    updated_at      timestamptz not null
);
-- The last version applied per question, either home (kept), and the events
-- seen (event_id, pruned after 30 days). `outcome` says what was done with it;
-- `detail` holds the audit for a press (who, by Slack id), never shown or logged.
create table if not exists partner_version_seen (
    source      text primary key,
    version     integer not null,
    updated_at  timestamptz not null
);
create table if not exists partner_event_seen (
    event_id    text primary key,
    source      text not null,
    version     integer not null,
    kind        text not null,
    outcome     text not null,
    detail      jsonb,
    seen_at     timestamptz not null
);
create index if not exists partner_event_seen_at_idx on partner_event_seen (seen_at);

-- Every version kept, stored as zstd deltas (docs/agent/specs/2026-10-26-delta-versions-design.md,
-- 26 Oct 2026). `body` keeps the plain text of the CURRENT version only; an older version is
-- blanked ('') once `packed` holds it: a full copy (`zstd1-full`) at version 1 and every
-- hundredth after, else a delta against n - 1 (`zstd1-delta`, `base_n`). `body_bytes` and
-- `body_sha256` describe the plain text, whatever is stored. All null on a version written
-- before this change and not yet packed (artifacts-admin pack-versions).
alter table version add column if not exists packed bytea;
alter table version add column if not exists codec text;
alter table version add column if not exists base_n integer;
alter table version add column if not exists body_bytes integer;
alter table version add column if not exists body_sha256 text;
-- Already compressed: TOAST must not try again.
alter table version alter column packed set storage external;

-- WHICH SESSION HOLDS A WAIT RIGHT NOW (29 Oct 2026, the "where an answer goes" slice).
-- `last_read_at` is stamped at the START of every /events read, a plain poll
-- included, so it cannot tell a listener from a glance. `held_until` is set only
-- by a read that holds (`wait` > 0): its start plus the hold plus a short grace,
-- and pulled back to "now" when the hold returns. Null: never held.
alter table seat_read add column if not exists held_until timestamptz;

-- AN AGENT'S OWN WORDS FOR "WHEN DO YOU ACT ON AN ANSWER?" (29 Oct 2026). One short
-- line per agent, declared and never verified; the page shows it as
-- "<agent> says: ..." in place of "acted on at its next check". Per agent (key)
-- because the service has no office entity.
create table if not exists agent_pickup (
    agent      text primary key,
    pickup     text not null,
    updated_at timestamptz not null
);

-- PICTURES ON A COMMENT (pinned comments, slice 3, 29 Oct 2026;
-- docs/agent/specs/2026-10-29-pinned-comments.md section 6). One row per picture, the REWRITTEN
-- file (images.py keeps only the chunks that draw it: no EXIF, no text). `comment_id` is null
-- while the picture is staged (uploaded, not yet attached to a comment): `staged_by` then
-- names the uploader and `created_at` starts the hour after which the sweep drops it.
-- Deleting a page or a comment deletes its pictures with it. Not the `file` table (that is
-- per page version and served as page content) and not `packing` (already compressed).
create table if not exists comment_image (
    id           text primary key,
    artifact_id  text not null references artifact(id) on delete cascade,
    comment_id   text references comment(id) on delete cascade,
    position     integer check (position between 1 and 4),
    staged_by    text,
    content_type text not null
        check (content_type in ('image/png', 'image/jpeg', 'image/webp')),
    width        integer not null,
    height       integer not null,
    byte_size    integer not null,
    sha256       text not null,
    bytes        bytea not null,
    created_at   timestamptz not null default now(),
    unique (comment_id, position)
);
alter table comment_image alter column bytes set storage external;
create index if not exists comment_image_artifact_idx on comment_image (artifact_id);
create index if not exists comment_image_staged_idx
    on comment_image (staged_by, created_at) where comment_id is null;

-- SECRETS THE SERVICE MAKES FOR ITSELF, by name, so every replica and every restart
-- agrees on them (the picture links' signing key, images.py). Made on first use.
create table if not exists server_secret (
    name       text primary key,
    value      text not null,
    created_at timestamptz not null default now()
);

-- FORM CONTROLS INSIDE A PAGE (slice 1, 2 Nov 2026;
-- docs/agent/specs/2026-10-29-form-controls-design.md). A `data-artifact-form` subtree in a page
-- becomes ONE decision of kind 'form', keyed by (page, form_name) and kept across republishes;
-- its question column holds the form's title and its options '[]'. The kind CHECK is widened
-- here (SQLite does it as its schema step 11). The fields live per version in decision_field:
-- `spec` is the parsed field (name, type, label, options, required, limits); decision_form
-- carries the form-level flag (data-complete="all"). Nothing answers a form yet.
alter table decision add column if not exists form_name text;
alter table decision drop constraint if exists decision_kind_check;
alter table decision add constraint decision_kind_check
    check (kind in ('question', 'review', 'form'));
create index if not exists decision_form_name_idx on decision (artifact_id, form_name)
    where kind = 'form';
create table if not exists decision_form (
    decision_id  text not null references decision(id) on delete cascade,
    version_n    integer not null,
    complete_all boolean not null default false,
    position     integer not null default 0,
    primary key (decision_id, version_n)
);
create table if not exists decision_field (
    decision_id text not null references decision(id) on delete cascade,
    version_n   integer not null,
    position    integer not null,
    name        text not null,
    spec        jsonb not null,
    primary key (decision_id, version_n, name)
);

-- ARCHIVING SETS A PAGE'S QUESTIONS ASIDE (2 Nov 2026;
-- docs/agent/specs/2026-11-02-archive-sets-aside-design.md; Jeremy's rule of 2 Nov replaces
-- his 21 Sep one that closed them). "Set aside" is DERIVED, never stored: a question is set
-- aside exactly when it is open (no answer, not closed) and its page is archived or binned.
-- The one new column is `reopened_at`, stamped when the page comes back and the question
-- with it. The event kinds `decision_set_aside` and `decision_reopened` are in the CHECK
-- above (`decision_shelved` stays for history and is no longer written); SQLite does both
-- as its schema step 12.
alter table decision add column if not exists reopened_at timestamptz;

-- A form's submitted answers (forms.submit): one merged record per form decision,
-- its history inside it. Null until he submits. `answer` and `answer_note` stay
-- null for a form; `answered_at`/`answered_by` move with every submit.
alter table decision add column if not exists answer_record jsonb;

-- FOLDERS (2 Nov 2026; docs/agent/specs/2026-11-02-folders-design.md, slice 1). A page is in at
-- most one folder, or none. `owner` is an agent name, or the reserved `jeremy` for a folder he
-- makes; never null, because a unique index over a nullable column treats nulls as distinct.
-- `artifact.agent` is untouched: the publisher stays on every page, so a folder of his can hold
-- several agents' pages. A folder is a shelf fact like archive and pin: it makes no version.
-- SQLite does the same as its schema step 14.
create table if not exists folder (
    id          text primary key,
    owner       text not null,
    slug        text not null,
    title       text not null,
    archived_at timestamptz,
    created_at  timestamptz not null default now(),
    unique (owner, slug)
);
create table if not exists folder_alias (
    owner     text not null,
    old_slug  text not null,
    folder_id text not null references folder(id) on delete cascade,
    primary key (owner, old_slug)
);
alter table artifact add column if not exists folder_id text references folder(id);
create index if not exists artifact_folder_idx on artifact (folder_id) where folder_id is not null;

-- FOLDER ARCHIVE (2 Nov 2026, slice 2): the folder a page was archived THROUGH, set on exactly the
-- pages a folder archive archived and cleared by every page-level archive, unarchive and move.
-- Unarchiving the folder restores only the pages that still carry its id. No foreign key: it is a
-- marker, and the rename merge repoints it by hand. SQLite does the same as its schema step 15.
alter table artifact add column if not exists archived_via_folder text;
create index if not exists artifact_archived_via_folder_idx on artifact (archived_via_folder)
    where archived_via_folder is not null;

-- DELIVERY RECEIPTS, slice 1 (4 Nov 2026; docs/agent/specs/2026-11-04-receipts-design.md).
-- What an agent says it did with his answer. Whether it has SEEN a decision stays
-- `decision.acknowledged_at/by` (first wins, cleared on reopen); a `seen` row is written
-- beside it only to carry the session. Every state is an append-only row here; the CURRENT receipt is the latest row that is not voided.
-- `voided_at` is stamped on every row of a subject when a new human act reopens it (a form
-- submit or relay, a review sent back), so an old "done" never covers a new answer; the
-- rows stay as history. `expires_at` is set for `working` only; a lapsed one is computed on
-- read. `subject_kind` is 'decision' now, 'thread' in slice 4. The event kind `receipt_set`
-- is in the event CHECK above (SQLite: schema step 16).
create table if not exists receipt (
    id           text primary key,
    subject_kind text not null check (subject_kind in ('decision', 'thread')),
    subject_id   text not null,
    state        text not null check (state in ('seen', 'working', 'blocked', 'wont_do', 'done')),
    note         text not null default '',
    link         text,
    agent        text not null,
    session      text,
    comment_id   text,
    created_at   timestamptz not null default now(),
    expires_at   timestamptz,
    voided_at    timestamptz
);
create index if not exists receipt_subject_idx
    on receipt (subject_kind, subject_id, created_at);

-- A `done` receipt on a decision also writes an outcome row; this is its link.
alter table decision_outcome add column if not exists link text;

-- GENERATED PAGES (4 Nov 2026; docs/agent/specs/2026-11-04-generated-pages-design.md, slice 1).
-- Four nullable shelf facts, like archive and pin: they make no version and never touch
-- `updated_at`. `refresh_every` is the interval the publisher declared, in seconds (null is no
-- promise and no flag); `checked_at` is the last time the publisher vouched for the page (a touch,
-- any publish, declaring an interval), set from the service's clock; `check_status` is that
-- vouching's verdict and `check_note` its one line (200 characters). Whether a page has lapsed is
-- worked out in Python from these and the live version's `created_at`, never in SQL.
-- SQLite does the same as its schema step 17.
alter table artifact add column if not exists refresh_every integer;
alter table artifact add column if not exists checked_at timestamptz;
alter table artifact add column if not exists check_status text check (check_status in ('ok', 'failed'));
alter table artifact add column if not exists check_note text;

-- SERVICE META (5 Nov 2026; docs/agent/specs/2026-11-04-core-and-plugins-design.md, 3.2, slice 1).
-- Facts about the database itself, one row per key. `owner_id` is the principal id the owner's own
-- acts are stored under (`by`, `author`, `answered_by`, `actor`, a folder's owner): recorded at the
-- first start, compared at every later one, so a pod started with another id refuses instead of
-- reading the owner's rows as a stranger's. `artifacts-admin change-owner` is the only writer after
-- that. SQLite does the same as its schema step 19.
create table if not exists service_meta (
    key        text primary key,
    value      text not null,
    updated_at timestamptz not null default now()
);

-- THE CORE OUTBOX (6 Nov 2026; docs/agent/specs/2026-11-04-core-and-plugins-design.md, 4.1, slice 4).
-- One row per sink per event, written in the action's own transaction by `events.emit` and drained
-- by the core sender thread (`sinks.py`): at-least-once, backoff, parked after MAX_ATTEMPTS and
-- never dropped. `event_json` is the whole `events.Event`; its `id` is the sink's idempotency key.
-- A row whose sink is not registered is never claimed. `partner_outbox` is NOT migrated into this
-- table: its per-question versions and ordering are the partner bridge's own, its sender still
-- drains it, and rows written before this table existed keep delivering untouched.
-- SQLite does the same as its schema step 20.
create table if not exists outbox (
    id            text primary key,
    sink          text not null,
    event_json    jsonb not null,
    created_at    timestamptz not null,
    attempts      integer not null default 0,
    next_at       timestamptz not null,
    claimed_until timestamptz,
    delivered_at  timestamptz,
    parked_at     timestamptz,
    last_error    text,
    last_error_at timestamptz
);
create index if not exists outbox_due_idx
    on outbox (next_at) where delivered_at is null and parked_at is null;

-- SHARED PLUG-IN STATE (8 Nov 2026; core-and-plugins design 4.5, slice 7b part 5). The per-sink
-- circuit breaker and the plug-in failure counts, kept here so every worker process of a
-- deployment sees one breaker per sink and one count per plug-in interface. `failed_keys` is the
-- (at most two) distinct outbox row ids that failed, newline-separated.
create table if not exists sink_breaker (
    sink        text primary key,
    failures    integer not null default 0,
    open_until  timestamptz,
    failed_keys text not null default '',
    opened_at   timestamptz
);
create table if not exists plugin_failure (
    plugin    text not null,
    interface text not null,
    failures  integer not null default 0,
    primary key (plugin, interface)
);

-- MCP's `whats_new` cursor for an agent key (docs/agent/specs/2026-11-08-mcp-oauth-design.md,
-- 11.3): a chat does not remember where it last looked, so the server does, one row per key.
-- `seq` is the last event number the key was shown; no row means it has never looked.
create table if not exists mcp_events_seen (
    key_id     text primary key,
    seq        bigint not null,
    updated_at timestamptz not null
);

-- An app that may ask to connect over MCP (docs/agent/specs/2026-11-08-mcp-oauth-design.md, 5
-- and 8): a fetched client metadata document (cached; `id` is its URL) or a self-registered
-- client (`id` is 'reg_' + random). Public clients only: never a secret. `host` is the checked
-- address it is shown by, `name` its own claim. `registered_from` is the address (an IPv6
-- one as its /64) a registration came from, for the hourly limit.
create table if not exists mcp_client (
    id              text primary key,
    kind            text not null,
    host            text not null,
    name            text not null default '',
    redirect_uris   jsonb not null,
    created_at      timestamptz not null default now(),
    fetched_at      timestamptz,
    registered_from text,
    last_used_at    timestamptz
);
create index if not exists mcp_client_registered_idx
    on mcp_client (registered_from, created_at) where kind = 'registered';

-- The agent name each person-and-app pair publishes under (docs/agent/specs/
-- 2026-11-08-mcp-oauth-design.md, 8 and 11.1): `<person>--<host slug>`, `-2`, `-3` for a
-- second app on the same host. Never deleted: pages keep it after the app is revoked, and a
-- later consent from the same app reuses it. `host` is the app's checked address, shown as
-- "<owner name>, via <host>".
create table if not exists mcp_agent (
    agent      text primary key,
    person     text not null,
    client_id  text not null,
    host       text not null,
    created_at timestamptz not null default now()
);
create unique index if not exists mcp_agent_pair on mcp_agent (person, client_id);

-- One person's consent for one app (cove's "connected app"; spec 8). `id` is 'app_' + random,
-- the public handle; `agent` the person-and-app agent above; `permissions` a subset of
-- ["read","write","relay"]; `idle_expires_at` slides 30 days on each refresh, `expires_at`
-- is the absolute cap, 90 days from consent; `revoked_reason` is 'revoked' | 'owner' | 'app' | 'reuse'
-- | 'lapsed' | 'admin'; `events_seen` is whats_new's cursor. One live app per person and app.
create table if not exists mcp_app (
    id              text primary key,
    person          text not null,
    client_id       text not null,
    agent           text not null,
    permissions     jsonb not null,
    redirect_uri    text not null,
    groups          jsonb not null default '[]',
    created_at      timestamptz not null default now(),
    last_used_at    timestamptz,
    idle_expires_at timestamptz not null,
    expires_at      timestamptz not null,
    revoked_at      timestamptz,
    revoked_reason  text,
    events_seen     bigint not null default 0
);
create unique index if not exists mcp_app_live
    on mcp_app (person, client_id) where revoked_at is null;

-- Tokens as SHA-256 hashes only (spec 8, 9). A refresh token is used once.
create table if not exists mcp_token (
    token_sha256 text primary key,
    app_id       text not null references mcp_app(id) on delete cascade,
    kind         text not null,
    created_at   timestamptz not null default now(),
    expires_at   timestamptz not null,
    used_at      timestamptz
);
create index if not exists mcp_token_app on mcp_token (app_id);

-- One sign-in in flight, from /oauth/authorize to the code exchange (spec 8). Every secret
-- in it is a hash or server-side only; each stage is single-use; dead after ten minutes.
-- `browser_sha256` binds the upstream state to the browser that started it (a cookie).
create table if not exists mcp_signin (
    id                text primary key,
    client_id         text not null,
    redirect_uri      text not null,
    code_challenge    text not null,
    resource          text not null,
    scope             text not null default '',
    client_state      text,
    oidc_state_sha256 text unique,
    browser_sha256    text,
    oidc_nonce        text,
    oidc_verifier     text,
    person            text,
    groups            jsonb not null default '[]',
    consent_sha256    text unique,
    code_sha256       text unique,
    code_expires_at   timestamptz,
    app_id            text,
    permissions       jsonb not null default '[]',
    created_at        timestamptz not null default now(),
    expires_at        timestamptz not null,
    used_at           timestamptz
);

-- ASKING ON ANY PAGE HE CAN READ (9 Nov 2026; MCP sign-in design 11.4). Null: the question
-- belongs to its page's agent, as every question did before. Set: a signed-in app asked on a
-- page that is not its own, and `asked_by` is that app's agent; the answer goes to it, the
-- page's agent is told only that a question was put on its page, and only the asker may close
-- it. SQLite does the same as its schema step 27.
alter table decision add column if not exists asked_by text;

-- THREE MORE KINDS OF COMMENT (9 Nov 2026): `explain`, `shorten` and `fix` join comment, remove
-- and looks_good. The kind is validated in code (`comments.KINDS`) so the next one needs no
-- migration: drop the CHECK a database still has on comment.kind, found by what it tests.
-- SQLite does the same as its schema step 28. Idempotent.
do $$
declare c record;
begin
    for c in
        select con.conname
        from pg_constraint con
        where con.contype = 'c'
          and con.conrelid = 'comment'::regclass
          and pg_get_constraintdef(con.oid) ~ '^CHECK \(\(?kind\M'
    loop
        execute format('alter table comment drop constraint %I', c.conname);
    end loop;
end $$;

-- WHETHER COMMENTING STARTS ON OR OFF ON A PAGE (9 Nov 2026). The publishing agent chooses:
-- "off" means the ways in start off for a reader (no pill on selected words, nothing inviting
-- a comment) and the reader can turn commenting on for that page. Null: the publisher did not
-- say, which a reader's page treats as on. Nullable, no default, so "not stated" is recorded.
alter table artifact add column if not exists comments_default text
    check (comments_default in ('on', 'off'));

"""Who an agent belongs to, who owns its pages, and how it is shown (agents design, slice 1;
docs/agent/specs/2026-11-26-agents-and-watching-design.md).

An agent is personal (it acts for one person) or shared (it is the deployment's). Today only an
app agent (`mcp_agent`) is personal, and so is a key agent the owner marked with an `agent_person`
row (slice 4); every other key agent is shared.
Nothing is copied: the answers are derived from `mcp_agent`, so an older image after a rollback
reads the same rows.

* `person_of(conn, agent)`: the person the agent belongs to, or None (shared).
* `page_owners(conn, agent)`: who owns that agent's pages: its person, or `shared_owner()`.
* `shown(conn, agent)`: the via line. `Shown.text()` is the only place `", via "` is written
  (`tests/test_via_line.py` refuses any other).
* `owner_sql(agent_col)`: the SQL twin of `page_owners`, read from `mcp_agent` and `service_meta`.

`agent` fields stored or returned anywhere stay the agent's name; `Shown` is for display only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ream import owner, reach, settings, text

NAME_LIMIT = 64


def shared_owner() -> str:
    """Who owns a shared agent's pages: the deployment's owner today (the team's maintainers in
    step 4 of the design). The one function that changes for this question."""
    return settings.owner_id()


def person_of(conn, agent: str) -> str | None:
    """The person `agent` belongs to, or None for a shared agent: an app agent's person
    (`mcp_agent`), else the person a key agent was marked for (`agent_person`)."""
    row = conn.execute(
        "select person from mcp_agent where agent = %s", (agent,)
    ).fetchone()
    if row is None:
        row = conn.execute(
            "select person from agent_person where agent = %s", (agent,)
        ).fetchone()
    return None if row is None else row[0]


class Refused(Exception):
    """A change of an agent's person the service will not make, with a reason fit to print."""


def set_person(db, agent: str, person: str, by: str) -> bool:
    """Mark the KEY agent `agent` as `person`'s machine (agents design, slice 4). Returns True when
    this changed anything. Only `artifacts-admin` and the owner's Machines page call it: no
    /api/v1 or MCP route does (`tests/test_personal_key.py` walks the routes). Raises `Refused`
    for an agent no live key holds, an app agent (its person is the one that signed in), or a person
    who does not exist."""
    now = datetime.now(UTC)
    with db.connect() as conn, conn.transaction():
        if conn.execute(
            "select 1 from mcp_agent where agent = %s", (agent,)
        ).fetchone():
            raise Refused(f"{agent} is an app agent: it already belongs to its person")
        if not conn.execute(
            "select 1 from agent_key where agent = %s and revoked_at is null limit 1",
            (agent,),
        ).fetchone():
            raise Refused(f"no agent named {agent} holds a live key")
        if not conn.execute("select 1 from person where id = %s", (person,)).fetchone():
            raise Refused(f"no person with id {person!r}")
        before = conn.execute(
            "select person from agent_person where agent = %s", (agent,)
        ).fetchone()
        if before is not None and before[0] == person:
            return False
        conn.execute(
            "insert into agent_person (agent, person, set_at, set_by) values (%s, %s, %s, %s)"
            " on conflict (agent) do update set person = excluded.person,"
            " set_at = excluded.set_at, set_by = excluded.set_by",
            (agent, person, now, by),
        )
    return True


def clear_person(db, agent: str) -> bool:
    """Make `agent` shared again (it reads as a key at once). True when it was personal."""
    with db.connect() as conn, conn.transaction():
        return (
            conn.execute("delete from agent_person where agent = %s", (agent,)).rowcount
            > 0
        )


def key_reach(conn, agent: str) -> reach.Reach:
    """The reach of a KEY acting as `agent`: exactly `reach.of_key` for a shared key. A personal
    key (an `agent_person` row) READS as its person does (`access.reads_sql`: its person's
    personal agents' pages, its own and pages open to agents; the owner's key reads what the
    owner reads) but CHANGES only its own pages (decision D: `writable_agents` stays empty, and
    nothing that asks or relays takes `reads_person()` for a key)."""
    row = conn.execute(
        "select person from agent_person where agent = %s", (agent,)
    ).fetchone()
    if row is None:
        return reach.of_key(agent)
    return reach.Reach(agent=agent, readable=reach.READS_AS_PERSON, person=row[0])


def page_owners(conn, agent: str) -> tuple[str, ...]:
    """Who owns the pages `agent` publishes (and so who a question with no `addressed_to` goes to)."""
    person = person_of(conn, agent)
    return (shared_owner(),) if person is None else (person,)


def owner_sql(agent_col: str) -> str:
    """SQL for who owns the pages of the agent in `agent_col`: the person of a personal agent, else
    the owner the database records (`service_meta`, written by `owner.check`; the SQL twin of
    `page_owners`, whose shared owner is the same id because `owner.check` refuses a start with
    another). No owner id is written into the text and no parameter is added, so the callers'
    positional parameters stay as they are in the long wall queries."""
    return (
        "coalesce((select m.person from mcp_agent m"  # noqa: S608 -- fixed columns and key
        f" where m.agent = {agent_col}),"
        f" (select k.person from agent_person k where k.agent = {agent_col}),"
        f" (select s.value from service_meta s where s.key = '{owner.META_KEY}'))"
    )


def person_label(person_id: str, stored_name: str | None) -> str:
    """The name shown for a person: `owner_name` for the owner (the rule of `people._person`),
    otherwise their stored name, failing that their id. Sanitised and capped: display only."""
    s = settings.current()
    name = s.owner_name if person_id == s.owner_id else (stored_name or person_id)
    return text.sanitize(name, NAME_LIMIT) or person_id


@dataclass(frozen=True)
class Shown:
    """How an agent is shown. `agent` is the stored name. `person` is the id of the person it
    belongs to (None: shared) and `person_name` their display name; `via` is what it comes through
    (an app's host); `team` is None until step 4."""

    agent: str
    person: str | None = None
    person_name: str = ""
    via: str | None = None
    team: str | None = None
    machine: str | None = (
        None  # a personal key's agent name: its line is "<person>, via <name>"
    )

    def text(self) -> str:
        """The line `<person name>, via <host>` for a personal agent, else the agent's own name.
        Escape it where it is drawn."""
        if self.person is not None and self.via:
            return f"{self.person_name}, via {self.via}"
        if self.person is not None and self.machine:
            return f"{self.person_name}, via {self.machine}"
        return self.agent


def app_shown(agent: str, person_id: str, stored_name: str | None, host: str) -> Shown:
    """A person-and-app agent on `host`, for a caller that already holds the pieces."""
    return Shown(agent, person_id, person_label(person_id, stored_name), host)


def owner_app(host: str) -> Shown:
    """The owner's app on `host`, where no agent exists yet (the consent screen the owner reads
    before the app is signed in) or its record is gone."""
    return app_shown("", settings.owner_id(), None, host)


def unknown_app() -> Shown:
    """An answer passed on by an app whose host is no longer known: the owner's, via "an app"."""
    return owner_app("an app")


def shown(conn, agent: str) -> Shown:
    """The via line of `agent`: an app agent is "<its person>, via <host>", a personal key's agent
    (an `agent_person` row, slice 4) "<its person>, via <agent name>", any other is its name.
    A personal key's `via` stays None (that marks an app, which the gallery's asker and relay
    lines read), its name rides in `machine`."""
    row = conn.execute(
        "select m.person, m.host, p.name from mcp_agent m"
        " left join person p on p.id = m.person where m.agent = %s",
        (agent,),
    ).fetchone()
    if row is None:
        mine = conn.execute(
            "select k.person, p.name from agent_person k"
            " left join person p on p.id = k.person where k.agent = %s",
            (agent,),
        ).fetchone()
        if mine is None:
            return Shown(agent)
        return Shown(agent, mine[0], person_label(mine[0], mine[1]), None, None, agent)
    return app_shown(agent, row[0], row[2], row[1])
